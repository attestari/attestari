"""Tamper-evident audit chain (in-memory; no DB needed)."""

from __future__ import annotations

import dataclasses

from notari import Memory


def _seed() -> Memory:
    mem = Memory()
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I moved to Berlin.", subject_id="u1", valid_from="2026-03-01")
    return mem


def test_audit_chain_verifies() -> None:
    mem = _seed()
    report = mem.verify_audit()
    assert report.ok
    assert report.entries >= 4  # 2 episodes + asserts + an invalidation
    assert report.broken_at is None


def test_audit_detects_edit() -> None:
    mem = _seed()
    # Tamper with a stored audit entry's content digest.
    store = mem.store
    store._audit[1] = dataclasses.replace(store._audit[1], payload_hash="deadbeef")
    report = mem.verify_audit()
    assert not report.ok
    assert report.broken_at == 2


def test_audit_detects_deletion() -> None:
    mem = _seed()
    # Remove a middle entry -> the chain linkage breaks.
    del mem.store._audit[1]
    assert not mem.verify_audit().ok


def test_deep_verify_detects_silent_content_edit() -> None:
    from notari.events import FactAsserted

    mem = _seed()
    # Find a fact event and silently rewrite its object *in the log only*,
    # leaving the audit chain untouched — the sneaky tamper.
    log = mem.store._log
    idx = next(i for i, e in enumerate(log) if isinstance(e, FactAsserted))
    log[idx] = dataclasses.replace(log[idx], object="Pyongyang")

    # Chain-only verify is fooled (it never re-derives content) — the gap.
    assert mem.verify_audit().ok
    # Deep verify re-derives the digest and catches it at the tampered seq.
    deep = mem.verify_audit(deep=True)
    assert not deep.ok
    assert deep.broken_at == idx + 1  # seq is 1-based


def test_deep_verify_ok_when_untampered() -> None:
    mem = _seed()
    assert mem.verify_audit(deep=True).ok


def test_forget_appends_to_chain_and_stays_valid() -> None:
    mem = _seed()
    before = mem.verify_audit().entries
    mem.forget("u1")
    report = mem.verify_audit()
    assert report.ok and report.entries == before + 1  # the SubjectForgotten event
