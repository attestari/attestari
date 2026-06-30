"""MCP server — expose Notari to any agent framework that speaks MCP.

This is the distribution surface: an MCP-speaking agent (Claude, frameworks) can
remember, recall, trace provenance, and forget — over stdio.

    pip install "notari[server,postgres,crypto]"   # 'server' includes mcp
    NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari \
        python -m notari.mcp

The tool *logic* lives in plain `tool_*` functions (unit-testable without the mcp
package); `create_server` registers thin MCP wrappers around them.
"""

from __future__ import annotations

import os
from typing import Any

from .memory import Memory


def _memory() -> Memory:
    from .embed import default_embedder

    embedder = default_embedder()
    if os.environ.get("NOTARI_DATABASE_URL"):
        return Memory.postgres(embedder=embedder)
    return Memory(embedder=embedder)


# --- tool logic (plain functions; no MCP dependency) ---------------------- #

def tool_add(
    mem: Memory,
    text: str,
    subject_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    source_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "fact_ids": mem.add(
            text,
            subject_id=subject_id,
            agent_id=agent_id,
            session_id=session_id,
            source_ref=source_ref,
        )
    }


def tool_search(
    mem: Memory,
    query: str,
    subject_id: str | None = None,
    as_of: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    results = mem.search(query, subject_id=subject_id, as_of=as_of, limit=limit)
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


def tool_forget(mem: Memory, subject_id: str, requested_by: str = "mcp") -> dict[str, Any]:
    c = mem.forget(subject_id, requested_by=requested_by)
    return {
        "certificate_id": c.certificate_id,
        "facts_deleted": c.facts_deleted,
        "episodes_deleted": c.episodes_deleted,
        "manifest_hash": c.manifest_hash,
    }


# --- MCP wiring ----------------------------------------------------------- #

def create_server(memory: Memory | None = None):
    """Build the FastMCP server. Requires the `mcp` package."""
    from mcp.server.fastmcp import FastMCP

    mem = memory or _memory()
    server = FastMCP("notari")

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

    @server.tool()
    def forget_subject(subject_id: str, requested_by: str = "mcp") -> dict:
        """Right-to-be-forgotten: erase a subject; returns a deletion certificate."""
        return tool_forget(mem, subject_id, requested_by=requested_by)

    return server


def main() -> None:  # pragma: no cover - entrypoint
    create_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
