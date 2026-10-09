"""ProjectionCache: each process folds only what was appended since its last read.

Up to 0.0.7 every add(), search(), timeline() and forget() read and decrypted
the whole log. The backends now keep their last projection and ask the store
for the events since then. These tests check that the whole log is read once,
when an engine warms up, and that writes from other engines, forgets and
rolled-back writes all reach a cached projection. (The suite-wide check in
conftest.py also compares every cached projection with a full fold.)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from attestari import Memory
from attestari.store import LOG_START

DSN = os.environ.get("ATTESTARI_DATABASE_URL")
POSTGRES = pytest.param(
    "postgres",
    marks=pytest.mark.skipif(not DSN, reason="set ATTESTARI_DATABASE_URL to run Postgres tests"),
)


class _Rollback(Exception):
    pass


@pytest.fixture(params=["plain", "encrypted"])
def engine(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`engine()` opens another engine on the same log (its own connection and
    cache on SQLite and Postgres)."""
    if request.param == "encrypted":
        pytest.importorskip("cryptography")
        from attestari.crypto import generate_kek

        monkeypatch.setenv("ATTESTARI_KEK", generate_kek())
    else:
        monkeypatch.delenv("ATTESTARI_KEK", raising=False)
    tier = request.node.callspec.params["tier"]
    if tier == "memory":
        from attestari import InMemoryEventStore

        store = InMemoryEventStore()
        return lambda: Memory(store=store)
    if tier == "sqlite":
        return lambda: Memory.local(tmp_path / "log.db")
    from attestari import PostgresEventStore

    PostgresEventStore(DSN).truncate()
    return lambda: Memory.postgres(DSN)


def _cities(mem: Memory, subject_id: str) -> list[str]:
    return sorted(
        e.object
        for e in mem.timeline(subject_id=subject_id)
        if e.predicate == "lives_in" and e.alive
    )


@pytest.mark.no_projection_crosscheck  # the cross-check itself reads the whole log
@pytest.mark.parametrize("tier", ["memory", "sqlite", POSTGRES])
def test_after_warm_up_nothing_reads_the_whole_log(tier: str, engine) -> None:
    mem = engine()
    mem.add("My name is Bob. I live in Rome.", subject_id="bob")
    mem.add("I live in Berlin.", subject_id="alice")
    mem.timeline(subject_id="bob")  # warm up

    full_reads: list[str] = []
    store = mem.store
    events, changes_since = store.events, store.changes_since

    def counted_events():
        full_reads.append("events()")
        return events()

    def counted_changes_since(position):
        if position == LOG_START:
            full_reads.append("changes_since(LOG_START)")
        return changes_since(position)

    store.events, store.changes_since = counted_events, counted_changes_since
    fact_id = mem.add("I moved to Paris.", subject_id="alice")[0]
    mem.search("where does the user live", subject_id="alice")
    mem.answer("where does the user live", subject_id="bob")
    mem.timeline(subject_id="alice")
    mem.get_provenance(fact_id)
    mem.conflicts(subject_id="alice")
    mem.merge_entities("bob", "Bobby")
    mem.forget("bob")
    mem.is_forgotten("bob")

    assert full_reads == []


@pytest.mark.parametrize("tier", ["sqlite", POSTGRES])
def test_another_engines_writes_and_forgets_reach_a_cached_projection(tier: str, engine) -> None:
    reader, writer = engine(), engine()
    writer.add("I live in Berlin.", subject_id="alice")
    assert _cities(reader, "alice") == ["Berlin"]  # the reader caches alice

    writer.add("I moved to Paris.", subject_id="alice")
    writer.add("I live in Rome.", subject_id="bob")
    assert _cities(reader, "alice") == ["Paris"]
    assert _cities(reader, "bob") == ["Rome"]

    writer.forget("alice")
    assert reader.timeline(subject_id="alice") == []
    assert reader.is_forgotten("alice")
    assert not any(e.scope.subject_id == "alice" for e in reader._project().episodes.values())


@pytest.mark.parametrize("tier", ["memory", "sqlite", POSTGRES])
def test_a_rolled_back_write_does_not_stay_in_the_cache(tier: str, engine) -> None:
    mem = engine()
    mem.add("I live in Rome.", subject_id="bob")
    with pytest.raises(_Rollback):
        with mem.store.write_lock():
            mem.add("I moved to Paris.", subject_id="bob")
            assert _cities(mem, "bob") == ["Paris"]  # the cache saw the uncommitted write
            raise _Rollback

    assert _cities(mem, "bob") == ["Rome"]
    mem.add("I live in Oslo.", subject_id="carol")  # reuses the rolled-back seqs
    assert _cities(mem, "bob") == ["Rome"] and _cities(mem, "carol") == ["Oslo"]


@pytest.mark.parametrize("tier", ["memory"])
def test_provenance_of_a_forgotten_subjects_fact_is_gone(tier: str, engine) -> None:
    # Without a KEK this used to return the erased message's snippet.
    mem = engine()
    fact_id = mem.add("I live in Berlin.", subject_id="alice")[0]
    assert mem.get_provenance(fact_id).snippet == "Berlin"

    mem.forget("alice")

    assert mem.get_provenance(fact_id) is None
