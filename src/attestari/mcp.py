"""MCP server — expose Attestari to any agent framework that speaks MCP.

This is the distribution surface: an MCP-speaking agent (Claude, frameworks) can
remember, recall, trace provenance, and forget — over stdio.

    pip install "attestari[server,postgres,crypto]"   # 'server' includes mcp
    ATTESTARI_DATABASE_URL=postgresql://attestari:attestari@localhost:5433/attestari \
        python -m attestari.mcp

The tool *logic* lives in plain `tool_*` functions (unit-testable without the mcp
package); `create_server` registers thin MCP wrappers around them.

`forget_subject` is opt-in (ATTESTARI_MCP_ALLOW_FORGET=1). Erasure can't be
undone and an agent can be steered by text it reads, so the server doesn't offer
it unless the operator asks. When offered, it is flagged destructive and takes
two calls: a preview, then a confirmation that carries the preview's manifest hash.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

from .memory import ManifestChanged, Memory
from .records import DeletionCertificate
from .store import ForgottenSubjectError


def _memory() -> Memory:
    # Durable by default. MCP stdio servers are spawned and killed by the
    # client app (Claude Desktop restarts => new process), so ephemeral
    # storage would silently lose all memories on every app restart — the
    # opposite of a long-term memory product. Postgres when
    # ATTESTARI_DATABASE_URL is set; else a local SQLite file
    # (ATTESTARI_SQLITE_PATH or ~/.attestari/attestari.db). Extraction upgrades to
    # Claude automatically when ANTHROPIC_API_KEY is set.
    from .embed import default_embedder
    from .extract import default_extractor

    embedder = default_embedder()
    extractor = default_extractor()
    if os.environ.get("ATTESTARI_DATABASE_URL"):
        return Memory.postgres(embedder=embedder, extractor=extractor)
    return Memory.local(
        os.environ.get("ATTESTARI_SQLITE_PATH"), embedder=embedder, extractor=extractor
    )


# --- tool logic (plain functions; no MCP dependency) ---------------------- #

def tool_add(
    mem: Memory,
    text: str,
    subject_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    source_ref: str | None = None,
) -> dict[str, Any]:
    try:
        fact_ids = mem.add(
            text,
            subject_id=subject_id,
            agent_id=agent_id,
            session_id=session_id,
            source_ref=source_ref,
        )
    except ForgottenSubjectError:
        return {
            "status": "refused",
            "error": (
                "Nothing was stored: this subject's memory was erased, and it accepts no "
                "new content. Don't retry, and don't store it under another subject_id."
            ),
        }
    return {"fact_ids": fact_ids}


def tool_search(
    mem: Memory,
    query: str,
    subject_id: str | None = None,
    as_of: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    try:
        results = mem.search(query, subject_id=subject_id, as_of=as_of, limit=limit)
    except ValueError:
        # A malformed as_of should be a correctable tool error the agent can
        # read and retry, not a crashed tool call.
        return {"error": f"as_of must be an ISO date/datetime (e.g. 2026-01-01), got {as_of!r}"}
    return {
        "results": [
            {
                "score": r.score,
                "subject": r.edge.subject,
                "predicate": r.edge.predicate,
                "object": r.edge.object,
                "fact_id": r.edge.fact_id,
            }
            for r in results
        ]
    }


def tool_provenance(mem: Memory, fact_id: str) -> dict[str, Any]:
    p = mem.get_provenance(fact_id)
    if p is None:
        return {"error": "fact not found"}
    return {
        "fact_id": p.fact_id,
        "snippet": p.snippet,
        "source_ref": p.source_ref,
        "source_episode_id": p.source_episode_id,
    }


def _blast_radius(preview: DeletionCertificate) -> dict[str, Any]:
    return {
        "subject_id": preview.subject_id,
        "episodes": preview.episodes_deleted,
        "facts": preview.facts_deleted,
        "manifest_hash": preview.manifest_hash,
    }


def tool_forget(
    mem: Memory,
    subject_id: str,
    requested_by: str = "mcp",
    confirm_manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Erasure in two calls. Without `confirm_manifest_hash` it is a preview:
    nothing is destroyed, and the reply carries the counts and manifest hash.
    With that hash it erases, but only if the subject's records still match
    the preview."""
    if confirm_manifest_hash is None:
        preview = mem.forget(subject_id, requested_by=requested_by, dry_run=True)
        return {
            "status": "preview",
            **_blast_radius(preview),
            "next": (
                "Nothing was erased. Erasure is irreversible: show this to the user, and "
                "only with their explicit approval call forget_subject again with "
                "confirm_manifest_hash set to this manifest_hash."
            ),
        }
    try:
        c = mem.forget(
            subject_id, requested_by=requested_by, expected_manifest=confirm_manifest_hash
        )
    except ManifestChanged as e:
        return {
            "status": "refused",
            "error": (
                "The subject's records don't match that manifest_hash (they changed since "
                "the preview, or the hash is wrong). Nothing was erased. Below is the "
                "current preview; review it with the user before confirming again."
            ),
            **_blast_radius(e.preview),
        }
    return {
        "status": "erased",
        "certificate_id": c.certificate_id,
        "facts_deleted": c.facts_deleted,
        "episodes_deleted": c.episodes_deleted,
        "manifest_hash": c.manifest_hash,
        "signature": c.signature,
        "algorithm": c.algorithm,
    }


# --- MCP wiring ----------------------------------------------------------- #

def _destructive(fastmcp_cls: Any) -> dict[str, Any]:
    """`tool()` kwargs flagging a tool destructive, so MCP clients that honour
    tool annotations ask the user before each call. Empty on mcp releases
    without annotations; the opt-in and the two steps apply regardless."""
    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # pragma: no cover - mcp without tool annotations
        return {}
    if "annotations" not in inspect.signature(fastmcp_cls.tool).parameters:
        return {}  # pragma: no cover
    return {
        "annotations": ToolAnnotations(
            destructiveHint=True, readOnlyHint=False, openWorldHint=False
        )
    }


def create_server(memory: Memory | None = None, *, allow_forget: bool | None = None):
    """Build the FastMCP server. Requires the `mcp` package.

    `forget_subject` is registered only when `allow_forget` is true; left as
    None, it follows ATTESTARI_MCP_ALLOW_FORGET=1. See the module docstring."""
    from mcp.server.fastmcp import FastMCP

    mem = memory or _memory()
    if allow_forget is None:
        flag = os.environ.get("ATTESTARI_MCP_ALLOW_FORGET", "").strip().lower()
        allow_forget = flag in {"1", "true", "yes"}
    server = FastMCP("attestari")

    @server.tool()
    def add_memory(
        text: str,
        subject_id: str | None = None,
        agent_id: str | None = None,
        source_ref: str | None = None,
    ) -> dict:
        """Store a message in memory; extracts and remembers durable facts.
        Optionally tag it with the agent and a source reference (kept as provenance)."""
        return tool_add(mem, text, subject_id=subject_id, agent_id=agent_id, source_ref=source_ref)

    @server.tool()
    def search_memory(
        query: str, subject_id: str | None = None, as_of: str | None = None, limit: int = 5
    ) -> dict:
        """Recall facts relevant to a query, optionally as of a past date."""
        return tool_search(mem, query, subject_id=subject_id, as_of=as_of, limit=limit)

    @server.tool()
    def get_provenance(fact_id: str) -> dict:
        """Trace a remembered fact back to its source episode and exact snippet."""
        return tool_provenance(mem, fact_id)

    if allow_forget:

        @server.tool(**_destructive(FastMCP))
        def forget_subject(
            subject_id: str, requested_by: str = "mcp", confirm_manifest_hash: str | None = None
        ) -> dict:
            """Right-to-be-forgotten, in two calls. Irreversible.
            1. Call without confirm_manifest_hash: a preview. Nothing is erased;
               it returns the record counts and a manifest_hash.
            2. Show the preview to the user. Only with their explicit approval,
               call again with confirm_manifest_hash set to that hash: it erases
               exactly the previewed records (refused if they changed since) and
               returns a deletion certificate."""
            return tool_forget(
                mem, subject_id, requested_by=requested_by,
                confirm_manifest_hash=confirm_manifest_hash,
            )

    return server


def main() -> None:  # pragma: no cover - entrypoint
    create_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
