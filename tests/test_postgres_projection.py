"""The Postgres search tables, kept in step with the log a write at a time.

Up to 0.0.7 every write rebuilt the `edge` and `entity` tables from the whole
log. A write now updates only the rows its events touched, inside its own
transaction, and `projection_state` records which audit entry the tables
reflect. Each test checks the tables against a full rebuild: same rows, same
values.
"""

from __future__ import annotations

import os
import threading

import pytest

DSN = os.environ.get("ATTESTARI_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATTESTARI_DATABASE_URL to run Postgres tests")

from attestari import InMemoryProjectionBackend, Memory, PostgresEventStore  # noqa: E402
from attestari.crypto import generate_kek  # noqa: E402
from attestari.embed import HashEmbedder  # noqa: E402
from attestari.initdb import init_db  # noqa: E402


class _Rollback(Exception):
    pass


@pytest.fixture(params=["plain", "encrypted"])
def engine(request, monkeypatch: pytest.MonkeyPatch):
    if request.param == "encrypted":
        pytest.importorskip("cryptography")
        monkeypatch.setenv("ATTESTARI_KEK", generate_kek())
    else:
        monkeypatch.delenv("ATTESTARI_KEK", raising=False)
    PostgresEventStore(DSN).truncate()
    return lambda: Memory.postgres(DSN)


def _tables(mem: Memory) -> tuple[list[dict], list[dict]]:
    conn = mem.store._conn
    edges = conn.execute(
        """SELECT fact_id::text, subject, predicate, object, valid_from, valid_to, tx_from,
                  tx_to, confidence, source_episode::text, char_span_lo, char_span_hi,
                  subject_id, alive, embedding::text
             FROM edge ORDER BY fact_id"""
    ).fetchall()
    entities = conn.execute(
        "SELECT canonical_id, aliases FROM entity ORDER BY canonical_id"
    ).fetchall()
    return edges, entities


def _state(mem: Memory) -> dict | None:
    return mem.store._conn.execute("SELECT last_seq, last_hash FROM projection_state").fetchone()


def _assert_matches_a_rebuild(mem: Memory) -> None:
    incremental = _tables(mem)
    with mem.store.write_lock():
        type(mem.backend)._rebuild_tables(mem.backend)  # not counted by _count_rebuilds
    assert _tables(mem) == incremental


def _count_rebuilds(mem: Memory) -> list[int]:
    calls = [0]
    rebuild = mem.backend._rebuild_tables

    def counted() -> None:
        calls[0] += 1
        rebuild()

    mem.backend._rebuild_tables = counted
    return calls


def test_each_write_updates_the_tables_like_a_rebuild(engine) -> None:
    mem = engine()
    rebuilds = _count_rebuilds(mem)
    steps = [
        lambda: mem.add("My name is Bob. I live in Rome and I work at Acme.", subject_id="bob"),
        lambda: mem.add("I'm Alice. I live in Berlin. I use Python.", subject_id="alice"),
        lambda: mem.add("I moved to Paris. I use Rust.", subject_id="alice"),  # supersede + coexist
        lambda: mem.add("I live in Oslo.", subject_id="carol"),
        lambda: mem.merge_entities("bob", "Bob"),
        lambda: mem.merge_entities("bob", "Robert"),
        lambda: mem.unmerge_entities("bob", "Bob"),
        lambda: mem.forget("alice"),
        lambda: mem.add("I now work at Initech.", subject_id="bob"),
        lambda: mem.forget("nobody"),  # a forget that erases nothing
    ]
    for step in steps:
        step()
        assert rebuilds == [0]  # the write itself only touched its own rows
        _assert_matches_a_rebuild(mem)
    assert (
        mem.store._conn.execute(
            "SELECT count(*) AS n FROM edge WHERE subject_id = 'alice'"
        ).fetchone()["n"]
        == 0
    )
    assert mem.answer("where does the user live", subject_id="bob") == "Rome"
    assert _state(mem)["last_seq"] == len(mem.store.audit_entries())


def test_tables_catch_up_on_events_another_writer_left_out(engine) -> None:
    # A writer that doesn't update the tables, such as a 0.0.7 worker during an
    # upgrade or a script appending to the store directly.
    mem = engine()
    mem.add("I live in Rome.", subject_id="bob")
    store = PostgresEventStore(DSN)
    other = Memory(store=store, backend=InMemoryProjectionBackend(store, HashEmbedder()))
    other.add("I live in Berlin.", subject_id="alice")
    other.add("I moved to Paris.", subject_id="alice")
    other.forget("bob")

    rebuilds = _count_rebuilds(mem)
    mem.add("I live in Oslo.", subject_id="carol")

    assert rebuilds == [0]
    assert mem.answer("where does the user live", subject_id="alice") == "Paris"
    assert mem.answer("where does the user live", subject_id="bob") is None
    _assert_matches_a_rebuild(mem)


@pytest.mark.parametrize("state", ["wrong hash", "past the head", "missing"])
def test_a_state_off_the_chain_means_a_full_rebuild(engine, state: str) -> None:
    mem = engine()
    mem.add("I live in Rome.", subject_id="bob")
    conn = mem.store._conn
    if state == "wrong hash":
        conn.execute("UPDATE projection_state SET last_hash = 'not-on-the-chain'")
    elif state == "past the head":
        conn.execute("UPDATE projection_state SET last_seq = last_seq + 100")
    else:
        conn.execute("DELETE FROM projection_state")
    conn.execute("DELETE FROM edge")  # and the tables are wrong too

    rebuilds = _count_rebuilds(mem)
    mem.add("I live in Berlin.", subject_id="alice")

    assert rebuilds == [1]
    assert mem.answer("where does the user live", subject_id="bob") == "Rome"
    _assert_matches_a_rebuild(mem)


def test_attaching_rebuilds_tables_a_restore_left_empty(engine) -> None:
    # The README says to keep the derived tables out of backups; a restore then
    # brings back the log and the state row but not the tables.
    mem = engine()
    mem.add("I live in Rome.", subject_id="bob")
    mem.store._conn.execute("TRUNCATE edge, entity")

    fresh = engine()

    assert fresh.answer("where does the user live", subject_id="bob") == "Rome"
    _assert_matches_a_rebuild(fresh)


def test_a_rolled_back_write_leaves_the_tables_as_they_were(engine) -> None:
    mem = engine()
    mem.add("I live in Rome.", subject_id="bob")
    before, state = _tables(mem), _state(mem)

    with pytest.raises(_Rollback):
        with mem.store.write_lock():
            mem.add("I moved to Paris.", subject_id="bob")
            mem.forget("bob")
            raise _Rollback

    assert _tables(mem) == before and _state(mem) == state
    mem.add("I live in Oslo.", subject_id="carol")
    _assert_matches_a_rebuild(mem)


def test_concurrent_writers_leave_the_tables_like_a_rebuild(engine) -> None:
    engines = [engine() for _ in range(3)]
    errors: list[str] = []

    def write(k: int) -> None:
        try:
            for i in range(6):
                engines[k].add(f"I live in Town{k}x{i}. I use Tool{i}.", subject_id=f"s{i % 3}")
        except Exception as e:  # noqa: BLE001 - report every failure, not the first
            errors.append(repr(e))

    threads = [threading.Thread(target=write, args=(k,)) for k in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    _assert_matches_a_rebuild(engines[0])


def test_an_older_schema_still_reads_but_refuses_writes(engine) -> None:
    mem = engine()
    mem.add("I live in Rome.", subject_id="bob")
    mem.store._conn.execute("DROP TABLE projection_state")
    try:
        old = engine()
        assert old.answer("where does the user live", subject_id="bob") == "Rome"
        with pytest.raises(RuntimeError, match="initdb"):
            old.add("I live in Berlin.", subject_id="alice")
    finally:
        init_db(DSN)

    upgraded = engine()
    upgraded.add("I live in Berlin.", subject_id="alice")
    assert upgraded.answer("where does the user live", subject_id="alice") == "Berlin"
    _assert_matches_a_rebuild(upgraded)
