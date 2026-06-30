"""Predicate cardinality + conflict surfacing (in-memory)."""

from __future__ import annotations

from notari import Memory


def test_multivalued_predicates_coexist() -> None:
    # "uses" is multi-valued: both values should survive (no supersession).
    mem = Memory()
    mem.add("I use Python and I use Rust.", subject_id="u1", valid_from="2024-01-01")
    uses = [e for e in mem.timeline(subject_id="u1") if e.predicate == "uses"]
    objects = {e.object for e in uses if e.alive}
    assert objects == {"Python", "Rust"}


def test_singlevalued_predicates_supersede() -> None:
    # "lives_in" is single-valued: the new value supersedes the old.
    mem = Memory()
    mem.add("I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I moved to Berlin.", subject_id="u1", valid_from="2026-03-01")
    live = [e for e in mem.timeline(subject_id="u1") if e.predicate == "lives_in" and e.alive]
    assert len(live) == 1 and live[0].object == "Berlin"


def test_conflicts_are_surfaced() -> None:
    mem = Memory()
    mem.add("I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I moved to Berlin.", subject_id="u1", valid_from="2026-03-01")
    conflicts = mem.conflicts(subject_id="u1")
    lives = [c for c in conflicts if c["predicate"] == "lives_in"]
    assert len(lives) == 1
    values = {v["object"] for v in lives[0]["values"]}
    assert values == {"Delhi", "Berlin"}
    assert lives[0]["resolution"] == "recency"


def test_no_conflict_when_single_value() -> None:
    mem = Memory()
    mem.add("I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    assert mem.conflicts(subject_id="u1") == []
