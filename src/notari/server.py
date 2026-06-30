"""FastAPI server — the HTTP surface for the engine.

    pip install "notari[server,postgres]"
    NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari \
        uvicorn notari.server:app --reload

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
    # Deployment default: real embeddings when available (falls back to HashEmbedder).
    from .embed import default_embedder

    embedder = default_embedder()
    if os.environ.get("NOTARI_DATABASE_URL"):
        return Memory.postgres(embedder=embedder)
    return Memory(embedder=embedder)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


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


def create_app(memory: Memory | None = None) -> FastAPI:
    mem = memory or _default_memory()
    app = FastAPI(title="Notari", version="0.0.1")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/add")
    def add(req: AddRequest) -> dict[str, Any]:
        fact_ids = mem.add(
            req.text,
            subject_id=req.subject_id,
            agent_id=req.agent_id,
            session_id=req.session_id,
            org_id=req.org_id,
            valid_from=req.valid_from,
            source_ref=req.source_ref,
        )
        return {"fact_ids": fact_ids}

    @app.get("/v1/search")
    def search(
        q: str,
        subject_id: str | None = None,
        as_of: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        results = mem.search(q, subject_id=subject_id, as_of=as_of, limit=limit)
        return {"results": [{"score": r.score, "fact": _edge_dict(r.edge)} for r in results]}

    @app.get("/v1/timeline")
    def timeline(subject_id: str | None = None, subject: str | None = None) -> dict[str, Any]:
        edges = mem.timeline(subject=subject, subject_id=subject_id)
        return {"edges": [_edge_dict(e) for e in edges]}

    @app.get("/v1/provenance/{fact_id}")
    def provenance(fact_id: str) -> dict[str, Any]:
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

    @app.post("/v1/forget/{subject_id}")
    def forget(subject_id: str, requested_by: str = "system") -> dict[str, Any]:
        cert = mem.forget(subject_id, requested_by=requested_by)
        return {
            "certificate_id": cert.certificate_id,
            "subject_id": cert.subject_id,
            "requested_by": cert.requested_by,
            "episodes_deleted": cert.episodes_deleted,
            "facts_deleted": cert.facts_deleted,
            "manifest_hash": cert.manifest_hash,
            "issued_at": _iso(cert.issued_at),
        }

    @app.get("/v1/conflicts")
    def conflicts(subject_id: str | None = None) -> dict[str, Any]:
        return {"conflicts": mem.conflicts(subject_id=subject_id)}

    @app.get("/v1/audit/verify")
    def audit_verify() -> dict[str, Any]:
        r = mem.verify_audit()
        return {"ok": r.ok, "entries": r.entries, "head": r.head, "broken_at": r.broken_at}

    @app.get("/v1/graph")
    def graph(subject_id: str | None = None) -> dict[str, Any]:
        """All facts (current + superseded) as nodes+edges for the console. The
        client filters by the time slider using valid_from/valid_to."""
        edges = mem.timeline(subject_id=subject_id)
        nodes: dict[str, dict[str, str]] = {}
        for e in edges:
            nodes.setdefault(e.subject, {"id": e.subject, "label": e.subject, "kind": "entity"})
            nodes.setdefault(e.object, {"id": e.object, "label": e.object, "kind": "value"})
        return {"nodes": list(nodes.values()), "edges": [_edge_dict(e) for e in edges]}

    @app.get("/", response_class=HTMLResponse)
    def console() -> str:
        from .console import CONSOLE_HTML

        return CONSOLE_HTML

    return app


# Module-level app for `uvicorn notari.server:app`.
app = create_app()
