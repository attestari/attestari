"""Concurrent add() calls for one subject.

add() decides dedup and supersession from the live projection. Two calls for
the same subject used to read the projection before either wrote, so both
closed the old value and both asserted a new one, leaving two live values for a
single-valued predicate. The decision and its writes now run under the store's
write lock: concurrent calls take turns, and the later one supersedes the
earlier one's value.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from attestari import Memory
from attestari.extract import DeterministicExtractor


class _SlowExtractor:
    """Holds each call open long enough for a concurrent add() to overlap it,
    the way a Claude extraction call does."""

    def __init__(self, delay: float = 0.2) -> None:
        self._inner = DeterministicExtractor()
        self._delay = delay

    def extract(self, text, scope):
        facts = self._inner.extract(text, scope)
        time.sleep(self._delay)
        return facts


CITIES = ["Berlin", "Paris", "Lisbon", "Madrid"]


def _race(engines: list[Memory], texts: list[str]) -> None:
    errors: list[str] = []

    def run(mem: Memory, text: str) -> None:
        try:
            mem.add(text, subject_id="u1")
        except Exception as e:  # noqa: BLE001 - report every failure, not the first
            errors.append(repr(e))

    threads = [threading.Thread(target=run, args=(m, t)) for m, t in zip(engines, texts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def _lives_in(mem: Memory) -> tuple[list[str], int]:
    edges = [e for e in mem.timeline(subject_id="u1") if e.predicate == "lives_in"]
    return sorted(e.object for e in edges if e.alive), len(edges)


def test_concurrent_adds_on_one_engine_leave_one_live_value() -> None:
    mem = Memory(extractor=_SlowExtractor())
    mem.add("I live in Toronto.", subject_id="u1")
    _race([mem] * 3, [f"I live in {c}." for c in CITIES[:3]])

    live, total = _lives_in(mem)
    assert len(live) == 1 and total == 4  # one live value; Toronto and two others closed
    assert mem.verify_audit(deep=True).ok


def test_concurrent_identical_adds_assert_the_fact_once() -> None:
    mem = Memory(extractor=_SlowExtractor())
    _race([mem] * 3, ["I live in Berlin."] * 3)

    assert _lives_in(mem) == (["Berlin"], 1)  # deduplicated, not asserted three times


def test_concurrent_adds_across_engines_on_one_sqlite_file(tmp_path: Path) -> None:
    # Separate engines on one file behave like separate processes: each has its
    # own connection, and only the database lock orders them.
    path = tmp_path / "shared.db"
    engines = [Memory.local(path, extractor=_SlowExtractor()) for _ in range(3)]
    engines[0].add("I live in Toronto.", subject_id="u1")
    _race(engines, [f"I live in {c}." for c in CITIES[:3]])

    fresh = Memory.local(path)
    live, total = _lives_in(fresh)
    assert len(live) == 1 and total == 4
    assert fresh.verify_audit(deep=True).ok


def test_in_memory_store_keeps_one_chain_under_threads() -> None:
    mem = Memory()

    def write(k: int) -> None:
        for i in range(25):
            mem.add(f"I live in Town{k}x{i}.", subject_id=f"t{k}-{i}")

    threads = [threading.Thread(target=write, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert mem.verify_audit(deep=True).ok
    assert len(mem.timeline()) == 6 * 25


DSN = os.environ.get("ATTESTARI_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="set ATTESTARI_DATABASE_URL to run Postgres tests")
def test_concurrent_adds_across_postgres_engines() -> None:
    from attestari import PostgresEventStore

    store = PostgresEventStore(DSN)
    store.truncate()
    store.close()
    engines = [Memory.postgres(DSN, extractor=_SlowExtractor()) for _ in range(3)]
    engines[0].add("I live in Toronto.", subject_id="u1")
    _race(engines, [f"I live in {c}." for c in CITIES[:3]])

    fresh = Memory.postgres(DSN)
    live, total = _lives_in(fresh)
    assert len(live) == 1 and total == 4
    assert fresh.answer("where does the user live", subject_id="u1") == live[0]
    assert fresh.verify_audit(deep=True).ok
