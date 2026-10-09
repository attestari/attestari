"""MCP tools (logic is testable without the mcp package; server build is gated)."""

from __future__ import annotations

import pytest

from attestari import Memory
from attestari.mcp import tool_add, tool_forget, tool_provenance, tool_search


def test_mcp_tools_roundtrip() -> None:
    mem = Memory()
    tool_add(mem, "Hi, my name is Dana. I live in Delhi.", subject_id="u1")

    result = tool_search(mem, "where does the user live", subject_id="u1")
    assert result["results"][0]["object"] == "Delhi"

    fact_id = result["results"][0]["fact_id"]
    assert tool_provenance(mem, fact_id)["snippet"] == "Delhi"

    # Erasure takes two calls: a preview that destroys nothing...
    preview = tool_forget(mem, "u1")
    assert preview["status"] == "preview" and preview["facts"] >= 1
    assert tool_search(mem, "where does the user live", subject_id="u1")["results"]

    # ...then a confirmation carrying the preview's manifest hash.
    cert = tool_forget(mem, "u1", confirm_manifest_hash=preview["manifest_hash"])
    assert cert["status"] == "erased" and cert["facts_deleted"] == preview["facts"]
    assert tool_search(mem, "where does the user live", subject_id="u1")["results"] == []


def test_forget_refuses_when_records_changed_since_the_preview() -> None:
    mem = Memory()
    tool_add(mem, "Hi, my name is Dana. I live in Delhi.", subject_id="u1")
    preview = tool_forget(mem, "u1")
    tool_add(mem, "I work at Acme.", subject_id="u1")  # arrives after the user reviewed

    out = tool_forget(mem, "u1", confirm_manifest_hash=preview["manifest_hash"])
    assert out["status"] == "refused"
    assert out["facts"] == preview["facts"] + 1  # the current preview, for re-review
    assert not mem.is_forgotten("u1")
    assert tool_forget(mem, "u1", confirm_manifest_hash="made-up")["status"] == "refused"
    assert not mem.is_forgotten("u1")


def test_add_for_a_forgotten_subject_is_refused_not_a_crash() -> None:
    mem = Memory()
    tool_add(mem, "I live in Delhi.", subject_id="u1")
    preview = tool_forget(mem, "u1")
    tool_forget(mem, "u1", confirm_manifest_hash=preview["manifest_hash"])

    out = tool_add(mem, "I live in Berlin.", subject_id="u1")
    assert out["status"] == "refused" and "another subject_id" in out["error"]
    assert tool_search(mem, "where does the user live", subject_id="u1")["results"] == []


def test_forget_tool_is_opt_in_and_flagged_destructive(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from attestari.mcp import create_server

    def tools(**kw) -> dict:
        return {t.name: t for t in asyncio.run(create_server(Memory(), **kw).list_tools())}

    monkeypatch.delenv("ATTESTARI_MCP_ALLOW_FORGET", raising=False)
    assert "forget_subject" not in tools() and "add_memory" in tools()

    monkeypatch.setenv("ATTESTARI_MCP_ALLOW_FORGET", "1")
    forget = tools()["forget_subject"]
    assert forget.annotations is not None and forget.annotations.destructiveHint is True
    assert "forget_subject" not in tools(allow_forget=False)  # an explicit argument wins


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
    from attestari.mcp import create_server

    server = create_server(Memory())
    assert server is not None


def test_tool_search_reports_malformed_as_of() -> None:
    mem = Memory()
    out = tool_search(mem, "where", as_of="not-a-date")
    assert "error" in out and "as_of" in out["error"]
