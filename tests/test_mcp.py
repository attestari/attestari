"""MCP tools (logic is testable without the mcp package; server build is gated)."""

from __future__ import annotations

import pytest

from notari import Memory
from notari.mcp import tool_add, tool_forget, tool_provenance, tool_search


def test_mcp_tools_roundtrip() -> None:
    mem = Memory()
    tool_add(mem, "Hi, my name is Dana. I live in Delhi.", subject_id="u1")

    result = tool_search(mem, "where does the user live", subject_id="u1")
    assert result["results"][0]["object"] == "Delhi"

    fact_id = result["results"][0]["fact_id"]
    assert tool_provenance(mem, fact_id)["snippet"] == "Delhi"

    cert = tool_forget(mem, "u1")
    assert cert["facts_deleted"] >= 1
    assert tool_search(mem, "where does the user live", subject_id="u1")["results"] == []


def test_mcp_source_ref_flows_to_provenance() -> None:
    # add_memory now passes source_ref through, so provenance is no longer null.
    mem = Memory()
    res = tool_add(mem, "Hi, my name is Dana. I live in Delhi.", subject_id="u1", source_ref="msg-1")
    fact_id = res["fact_ids"][0]
    assert tool_provenance(mem, fact_id)["source_ref"] == "msg-1"


def test_provenance_missing_fact() -> None:
    assert "error" in tool_provenance(Memory(), "nope")


def test_mcp_server_builds() -> None:
    pytest.importorskip("mcp")
    from notari.mcp import create_server

    server = create_server(Memory())
    assert server is not None
