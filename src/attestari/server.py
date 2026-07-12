"""FastAPI server — the HTTP surface for the engine.

    pip install "attestari[server,postgres]"
    ATTESTARI_DATABASE_URL=postgresql://attestari:attestari@localhost:5433/attestari \
        uvicorn attestari.server:app --reload

Endpoints:
    POST /v1/add                 ingest a message
    GET  /v1/search              hybrid retrieval (+ as_of)
    GET  /v1/timeline            bi-temporal history for a subject/entity
    GET  /v1/provenance/{id}     trace a fact to its source
    POST /v1/forget/{subject}    right-to-be-forgotten (+ certificate)
    GET  /v1/graph               nodes+edges for the console
    GET  /healthz

This module imports FastAPI at top level — it is the optional `server` extra and
is never imported by the core package, so the engine stays dependency-free.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .memory import Memory
from .projection import Edge


def _default_memory() -> Memory:
    # Deployment defaults — "set the env var, get production behaviour":
    # real embeddings when the extra is installed; Claude extraction when
    # ANTHROPIC_API_KEY is set; durable storage — Postgres when
    # ATTESTARI_DATABASE_URL is set, else a local SQLite file (ATTESTARI_SQLITE_PATH
    # or ~/.attestari/attestari.db). A memory server whose memory vanishes on restart
    # is a broken promise; the ephemeral store stays via create_app(Memory()).
    from .embed import default_embedder
    from .extract import default_extractor

    embedder = default_embedder()
    extractor = default_extractor()
    if os.environ.get("ATTESTARI_DATABASE_URL"):
        return Memory.postgres(embedder=embedder, extractor=extractor)
    return Memory.local(
        os.environ.get("ATTESTARI_SQLITE_PATH"), embedder=embedder, extractor=extractor
    )


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _checked_iso(value: str | None, param: str) -> str | None:
    """Validate an ISO date/datetime query field up front so a malformed value
    is a 422 with a pointable message, not a 500 from deep in the engine."""
    if value is None:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{param} must be an ISO date/datetime (e.g. 2026-01-01), got {value!r}",
        ) from None
    return value


def _edge_dict(e: Edge) -> dict[str, Any]:
    return {
        "fact_id": e.fact_id,
        "subject": e.subject,
        "predicate": e.predicate,
        "object": e.object,
        "valid_from": _iso(e.valid_from),
        "valid_to": _iso(e.valid_to),
        "alive": e.alive,
        "confidence": e.confidence,
        "subject_id": e.subject_id,
        "source_episode_id": e.source_episode_id,
    }


class AddRequest(BaseModel):
    text: str
    subject_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    org_id: str | None = None
    valid_from: str | None = None
    source_ref: str | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "text": "Hi, my name is Dana. I live in Berlin and I work at Acme.",
                    "subject_id": "u1",
                    "valid_from": "2026-03-01",
                    "source_ref": "msg-001",
                }
            ]
        }
    }


def create_app(memory: Memory | None = None) -> FastAPI:
    mem = memory or _default_memory()
    app = FastAPI(
        title="Attestari",
        version="0.0.1",
        description=(
            "**The auditable memory layer for AI agents.** Every fact carries a "
            "receipt, the history is tamper-evident, and any subject's data can "
            "be provably deleted — crypto-shred plus a signed certificate.\n\n"
            "The interactive console (memory graph, time-travel slider, "
            "click-to-trace provenance) lives at [`/`](/)."
        ),
    )

    @app.get("/healthz", summary="Liveness check")
    def healthz() -> dict[str, str]:
        """Returns `{"status": "ok"}` when the server is up."""
        return {"status": "ok"}

    @app.post("/v1/add", summary="Ingest a message and remember its durable facts")
    def add(req: AddRequest) -> dict[str, Any]:
        """Store the raw message as an episode (the provenance record), extract
        durable facts from it, and remember them.

        Exact duplicates are skipped; a new value for a single-valued predicate
        (e.g. `lives_in`) **supersedes** the old one — the prior fact is closed,
        not erased, so history stays reconstructable. `valid_from` (ISO date)
        backdates when the facts became true in the world. Returns the ids of
        the newly asserted facts.
        """
        fact_ids = mem.add(
            req.text,
            subject_id=req.subject_id,
            agent_id=req.agent_id,
            session_id=req.session_id,
            org_id=req.org_id,
            valid_from=_checked_iso(req.valid_from, "valid_from"),
            source_ref=req.source_ref,
        )
        return {"fact_ids": fact_ids}

    @app.get("/v1/search", summary="Hybrid recall — now, or as of any past instant")
    def search(
        q: str,
        subject_id: str | None = None,
        as_of: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Semantic + keyword retrieval over the facts **valid at the requested
        instant**: omit `as_of` to query what's true now, or pass an ISO
        date/datetime to time-travel ("what did memory believe on that day?").
        `subject_id` scopes recall to one data subject. Every result carries its
        fact id, so provenance is always one call away.
        """
        if limit < 1:
            raise HTTPException(status_code=422, detail="limit must be >= 1")
        results = mem.search(
            q, subject_id=subject_id, as_of=_checked_iso(as_of, "as_of"), limit=limit
        )
        return {"results": [{"score": r.score, "fact": _edge_dict(r.edge)} for r in results]}

    @app.get("/v1/timeline", summary="Full bi-temporal history for a subject or entity")
    def timeline(subject_id: str | None = None, subject: str | None = None) -> dict[str, Any]:
        """Every fact — current **and** superseded — oldest first, each with its
        valid-time window and `alive` flag. This is the "show me how the memory
        evolved" view; corrections close windows, they never erase rows.
        """
        edges = mem.timeline(subject=subject, subject_id=subject_id)
        return {"edges": [_edge_dict(e) for e in edges]}

    @app.get("/v1/provenance/{fact_id}", summary="Trace a fact to its source")
    def provenance(fact_id: str) -> dict[str, Any]:
        """Where did this memory come from? Returns the source episode, the
        **exact snippet** of raw text the fact was extracted from (via its
        character span), the caller-supplied `source_ref`, and when it was
        learned.
        """
        p = mem.get_provenance(fact_id)
        if p is None:
            raise HTTPException(status_code=404, detail="fact not found")
        return {
            "fact_id": p.fact_id,
            "source_episode_id": p.source_episode_id,
            "recorded_at": _iso(p.recorded_at),
            "snippet": p.snippet,
            "source_ref": p.source_ref,
        }

    @app.post(
        "/v1/forget/{subject_id}",
        summary="Right-to-be-forgotten: crypto-shred + signed certificate",
    )
    def forget(subject_id: str, requested_by: str = "system") -> dict[str, Any]:
        """Erase one subject's entire scope (GDPR Art. 17). With encryption
        enabled (`ATTESTARI_KEK`), their per-subject key is **destroyed**, so the
        retained ciphertext is unrecoverable — including in backups of the
        event log. Returns a `DeletionCertificate` (counts + manifest hash):
        the proof that survives after the data is gone. With `ATTESTARI_KEK` set
        the certificate is **signed** (HMAC-SHA256 under a KEK-derived key —
        verify offline with `attestari.crypto.verify_certificate`); without it,
        `signature` is null. The audit chain remains verifiable either way.
        """
        cert = mem.forget(subject_id, requested_by=requested_by)
        return {
            "certificate_id": cert.certificate_id,
            "subject_id": cert.subject_id,
            "requested_by": cert.requested_by,
            "episodes_deleted": cert.episodes_deleted,
            "facts_deleted": cert.facts_deleted,
            "manifest_hash": cert.manifest_hash,
            "issued_at": _iso(cert.issued_at),
            "signature": cert.signature,
            "algorithm": cert.algorithm,
        }

    @app.get("/v1/conflicts", summary="Values that contradicted each other over time")
    def conflicts(subject_id: str | None = None) -> dict[str, Any]:
        """Single-valued predicates that held more than one distinct value
        (e.g. `lives_in`: Delhi → Berlin), with the full value history and how
        the conflict was resolved (recency). Conflicts are **surfaced**, never
        silently dropped.
        """
        return {"conflicts": mem.conflicts(subject_id=subject_id)}

    @app.get("/v1/audit/verify", summary="Verify the tamper-evident hash chain")
    def audit_verify(deep: bool = False) -> dict[str, Any]:
        """Walk the hash-linked audit chain over the event log. Any reorder,
        insertion, or deletion of history breaks a link and is reported at the
        exact `broken_at` seq. Verification uses content digests only — no PII —
        so it still passes after a subject is crypto-shredded.

        `deep=true` additionally re-derives every stored event's digest and
        re-hashes episode payloads against their `content_hash`, catching
        **silent in-place edits to event content** — not just ledger tampering.
        Deep verification survives sanctioned crypto-shreds and flags a rogue
        key deletion at its exact seq.
        """
        r = mem.verify_audit(deep=deep)
        return {
            "ok": r.ok,
            "entries": r.entries,
            "head": r.head,
            "broken_at": r.broken_at,
            "deep": deep,
        }

    @app.get("/v1/graph", summary="Nodes + edges for the console's time-slider view")
    def graph(subject_id: str | None = None) -> dict[str, Any]:
        """All facts (current + superseded) as a nodes+edges graph. The console
        filters client-side against `valid_from`/`valid_to` as the time slider
        moves.
        """
        edges = mem.timeline(subject_id=subject_id)
        nodes: dict[str, dict[str, str]] = {}
        for e in edges:
            nodes.setdefault(e.subject, {"id": e.subject, "label": e.subject, "kind": "entity"})
            nodes.setdefault(e.object, {"id": e.object, "label": e.object, "kind": "value"})
        return {"nodes": list(nodes.values()), "edges": [_edge_dict(e) for e in edges]}

    @app.get("/", response_class=HTMLResponse, summary="The Attestari console", include_in_schema=True)
    def console() -> str:
        """A single self-contained page: the memory graph with a time-travel
        slider, click-to-trace provenance, and a forget button."""
        from .console import CONSOLE_HTML

        return CONSOLE_HTML

    return app


# Module-level app for `uvicorn attestari.server:app`.
app = create_app()
