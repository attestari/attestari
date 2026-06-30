"""Tests for the core engine: bi-temporal recall, provenance, and deletion.

Run with `python -m pytest -q` (pyproject sets pythonpath=src).
"""

from __future__ import annotations

from notari import Memory


def _seed() -> Memory:
    mem = Memory()
    mem.add(
        "Hi, my name is Dana. I live in Delhi and I work at Acme.",
        subject_id="u1",
        valid_from="2019-01-01",
        source_ref="msg-1",
    )
    mem.add(
        "I moved to Berlin and I joined Globex.",
        subject_id="u1",
        valid_from="2026-03-01",
        source_ref="msg-2",
    )
    return mem


def test_current_recall() -> None:
    mem = _seed()
    assert mem.answer("where does the user live", subject_id="u1") == "Berlin"
    assert mem.answer("where does the user work", subject_id="u1") == "Globex"
    assert mem.answer("what is the user's name", subject_id="u1") == "Dana"


def test_bitemporal_asof() -> None:
    mem = _seed()
    # Same question, earlier instant -> the value that was true back then.
    assert mem.answer("where did the user live", subject_id="u1", as_of="2020-01-01") == "Delhi"
    assert mem.answer("where did the user work", subject_id="u1", as_of="2020-01-01") == "Acme"


def test_supersession_keeps_history() -> None:
    mem = _seed()
    cities = [e for e in mem.timeline(subject_id="u1") if e.predicate == "lives_in"]
    assert {e.object for e in cities} == {"Delhi", "Berlin"}
    delhi = next(e for e in cities if e.object == "Delhi")
    berlin = next(e for e in cities if e.object == "Berlin")
    assert not delhi.alive and delhi.valid_to is not None  # superseded, window closed
    assert berlin.alive and berlin.valid_to is None         # current, still open


def test_provenance_traces_to_source() -> None:
    mem = _seed()
    top = mem.search("where does the user live", subject_id="u1")[0]
    prov = mem.get_provenance(top.edge.fact_id)
    assert prov is not None
    assert prov.snippet == "Berlin"
    assert prov.source_ref == "msg-2"


def test_entity_merge_records_alias() -> None:
    mem = _seed()
    mem.merge_entities(canonical_id="u1", alias_id="the founder", evidence="same person")
    entity = mem._project().entities["u1"]
    assert "the founder" in entity.aliases


def test_forget_destroys_lineage_and_issues_certificate() -> None:
    mem = _seed()
    cert = mem.forget("u1", requested_by="dpo@example.com")
    assert cert.facts_deleted >= 3
    assert cert.episodes_deleted == 2
    assert len(cert.manifest_hash) == 64  # sha256 hex
    # Nothing left to retrieve for the subject.
    assert mem.answer("where does the user live", subject_id="u1") is None
    assert mem.timeline(subject_id="u1") == []


def test_dedup_noop_on_identical_readd() -> None:
    mem = Memory()
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    before = len(mem.timeline(subject_id="u1"))
    # Re-asserting the same live fact is a NOOP — no duplicate edge.
    mem.add("I live in Delhi.", subject_id="u1", valid_from="2019-06-01")
    assert len(mem.timeline(subject_id="u1")) == before
    assert mem.answer("where does the user live", subject_id="u1") == "Delhi"


def test_isolation_between_subjects() -> None:
    mem = _seed()
    mem.add("I'm Ravi and I live in Chennai.", subject_id="u2", valid_from="2021-01-01")
    # Forgetting u1 must not touch u2.
    mem.forget("u1")
    assert mem.answer("where does the user live", subject_id="u2") == "Chennai"
