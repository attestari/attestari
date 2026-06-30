"""Tests for the Notari LangChain integration (retriever + chat history)."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("langchain_core", reason="needs langchain-core")

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from notari import Memory  # noqa: E402
from notari_langchain import NotariChatMessageHistory, NotariRetriever  # noqa: E402


# -- NotariRetriever ----------------------------------------------------- #

def test_retriever_recalls_with_provenance():
    mem = Memory()
    mem.add("I live in Toronto and I work at Acme.", subject_id="alice")
    docs = NotariRetriever(mem=mem, subject_id="alice").invoke("where does the user live?")
    assert any("Toronto" in d.page_content for d in docs)
    d = docs[0]
    assert d.metadata["fact_id"] and d.metadata["source_episode_id"]
    assert "score" in d.metadata


def test_retriever_supersession():
    mem = Memory()
    mem.add("I live in Toronto.", subject_id="alice")
    mem.add("I moved to Berlin.", subject_id="alice")
    docs = NotariRetriever(mem=mem, subject_id="alice").invoke("where does the user live?")
    blob = " ".join(d.page_content for d in docs)
    assert "Berlin" in blob and "Toronto" not in blob


def test_retriever_time_travel_as_of():
    mem = Memory()
    mem.add("I live in Toronto.", subject_id="alice", valid_from="2021-01-01")
    mem.add("I moved to Berlin.", subject_id="alice", valid_from="2026-01-01")
    docs = NotariRetriever(mem=mem, subject_id="alice", as_of="2022-01-01").invoke(
        "where does the user live?"
    )
    assert any("Toronto" in d.page_content for d in docs)


def test_retriever_respects_k():
    mem = Memory()
    mem.add("I live in Toronto. I work at Acme. My name is Alice.", subject_id="alice")
    docs = NotariRetriever(mem=mem, subject_id="alice", k=1).invoke("tell me about the user")
    assert len(docs) <= 1


# -- NotariChatMessageHistory -------------------------------------------- #

def test_history_learns_and_surfaces_facts():
    mem = Memory()
    h = NotariChatMessageHistory(mem, subject_id="alice")
    h.add_messages([HumanMessage("I live in Toronto.")])
    h.add_messages([AIMessage("Noted.")])
    msgs = h.messages
    assert len(msgs) == 1 and "Toronto" in msgs[0].content


def test_history_empty_when_no_facts():
    h = NotariChatMessageHistory(Memory(), subject_id="nobody")
    assert h.messages == []


def test_history_clear_is_provable_deletion():
    mem = Memory()
    h = NotariChatMessageHistory(mem, subject_id="alice")
    h.add_messages([HumanMessage("I live in Toronto.")])
    h.clear()
    assert h.messages == []
    assert NotariRetriever(mem=mem, subject_id="alice").invoke("where?") == []


def test_subject_isolation():
    mem = Memory()
    NotariChatMessageHistory(mem, "alice").add_messages([HumanMessage("I live in Toronto.")])
    NotariChatMessageHistory(mem, "bob").add_messages([HumanMessage("I live in Berlin.")])
    bob_docs = NotariRetriever(mem=mem, subject_id="bob").invoke("where does the user live?")
    blob = " ".join(d.page_content for d in bob_docs)
    assert "Berlin" in blob and "Toronto" not in blob
