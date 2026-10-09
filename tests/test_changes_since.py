"""EventStore.changes_since: the events after a log position, checked against
the audit chain, on all three stores.

A projection cache folds in what this returns, so it has to return exactly the
events after the position, read the way events() reads them, and nothing when
the position is no longer on the chain.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from attestari import Memory
from attestari.store import LOG_START, LogPosition

DSN = os.environ.get("ATTESTARI_DATABASE_URL")

TIERS = [
    "memory",
    "sqlite",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(
            not DSN, reason="set ATTESTARI_DATABASE_URL to run Postgres tests"
        ),
    ),
]


class _Rollback(Exception):
    pass


@pytest.fixture(params=["plain", "encrypted"])
def mem(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tier = request.node.callspec.params["tier"]
    if request.param == "encrypted":
        pytest.importorskip("cryptography")
        from attestari.crypto import generate_kek

        monkeypatch.setenv("ATTESTARI_KEK", generate_kek())
    else:
        monkeypatch.delenv("ATTESTARI_KEK", raising=False)
    if tier == "memory":
        m = Memory()
    elif tier == "sqlite":
        m = Memory.local(tmp_path / "log.db")
    else:
        from attestari import PostgresEventStore

        PostgresEventStore(DSN).truncate()
        m = Memory.postgres(DSN)
    m.add("My name is Bob. I live in Rome.", subject_id="bob")
    m.add("I'm Alice. I live in Berlin. I use Python.", subject_id="alice")
    m.add("I moved to Paris.", subject_id="alice")
    m.merge_entities("bob", "Bobby")
    m.forget("bob")  # with a KEK, bob's episode and facts become unreadable
    m.add("I live in Oslo.", subject_id="carol")
    return m


def _positions(m: Memory) -> list[LogPosition]:
    return [LOG_START] + [LogPosition(e.seq, e.entry_hash) for e in m.store.audit_entries()]


@pytest.mark.parametrize("tier", TIERS)
def test_from_the_start_it_reads_what_events_reads(tier: str, mem: Memory) -> None:
    head, events = mem.store.changes_since(LOG_START)
    assert head == _positions(mem)[-1]
    assert events == mem.store.events()


@pytest.mark.parametrize("tier", TIERS)
def test_from_any_position_it_reads_the_events_after_it(tier: str, mem: Memory) -> None:
    full = mem.store.events()
    positions = _positions(mem)
    encrypted = mem.store.cipher.enabled
    previous = len(full)
    for k, position in enumerate(positions):
        head, events = mem.store.changes_since(position)
        assert head == positions[-1]
        if encrypted:
            # Erased events drop out, so what's after position k is a suffix
            # of the full read that shrinks as k grows.
            assert events == full[len(full) - len(events) :]
            assert len(events) <= previous
            previous = len(events)
        else:
            assert events == full[k:]
    assert mem.store.changes_since(positions[-1]) == (positions[-1], [])


@pytest.mark.parametrize("tier", TIERS)
def test_a_position_off_the_chain_reads_nothing(tier: str, mem: Memory) -> None:
    head = _positions(mem)[-1]
    assert mem.store.changes_since(LogPosition(3, "not-on-the-chain")) is None
    assert mem.store.changes_since(LogPosition(head.seq + 1, head.entry_hash)) is None
    assert mem.store.changes_since(LogPosition(0, "not-genesis")) is None


@pytest.mark.parametrize("tier", TIERS)
def test_a_rolled_back_position_is_off_the_chain(tier: str, mem: Memory) -> None:
    with pytest.raises(_Rollback):
        with mem.store.write_lock():
            mem.add("I moved to Lisbon.", subject_id="carol")
            uncommitted, _ = mem.store.changes_since(LOG_START)  # sees its own writes
            raise _Rollback

    assert mem.store.changes_since(uncommitted) is None
    mem.add("I moved to Madrid.", subject_id="carol")  # appends reuse those seqs
    assert mem.store.changes_since(uncommitted) is None
