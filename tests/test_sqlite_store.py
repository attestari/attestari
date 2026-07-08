"""SQLiteEventStore — the durable zero-infrastructure tier (Memory.local()).

Covers: exact round-trip fidelity for all six event types (unicode, empty
payloads, tuple spans, naive datetimes), persistence + deep verification across
reopen, SQL tampering caught at the exact seq, encryption at rest, sanctioned
crypto-shred surviving deep verify vs rogue key deletion failing it,
thread-safety (no chain forks), cross-connection locking on one file, file
permissions, and the durable-by-default entry-point switch.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from notari import HashEmbedder, Memory, SQLiteEventStore
from notari.events import (
    EntityMerged,
    EntityUnmerged,
    EpisodeIngested,
    FactAsserted,
    FactInvalidated,
    Scope,
    SubjectForgotten,
)


def _all_event_types() -> list:
    """One of each event type with every field exercised (incl. unicode,
    empty strings, spans, supersession links, full scope)."""
    t0 = datetime(2024, 5, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
    scope = Scope(subject_id="u1", agent_id="a1", session_id="s1", org_id="o1")
    return [
        EpisodeIngested(
            episode_id="ep-1", content_hash="c" * 64, payload="Grüße 👋 — I live in Zürich.",
            scope=scope, ingested_at=t0, source_ref="msg-Ω",
        ),
        EpisodeIngested(  # empty payload, minimal scope
            episode_id="ep-2", content_hash="d" * 64, payload="", scope=Scope(), ingested_at=t0,
        ),
        FactAsserted(
            fact_id="f-1", subject="u1", predicate="lives_in", object="Zürich",
            source_episode_id="ep-1", valid_from=t0, valid_to=None, confidence=0.87,
            char_span=(23, 29), scope=scope, recorded_at=t0,
        ),
        FactInvalidated(
            fact_id="f-1", reason="superseded", valid_to=t0, superseded_by="f-2",
            recorded_at=t0,
        ),
        EntityMerged(canonical_id="Acme Corp", alias_id="Acme", evidence="auto:0.9",
                     recorded_at=t0),
        EntityUnmerged(canonical_id="Acme Corp", alias_id="Acme", recorded_at=t0),
        SubjectForgotten(subject_id="u9", requested_by="dpo@example.com", recorded_at=t0),
    ]


def test_round_trip_fidelity_all_event_types(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "n.db")
    originals = _all_event_types()
    for ev in originals:
        store.append(ev)
    got = store.events()
    assert got == originals  # dataclass equality: every field byte-identical
    # tuple type restored (JSON would happily hand back a list)
    fact = next(e for e in got if isinstance(e, FactAsserted))
    assert isinstance(fact.char_span, tuple)
    store.close()


def test_persistence_and_deep_verify_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    mem = Memory.local(path)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I moved to Berlin.", subject_id="u1", valid_from="2026-03-01")
    n_events = len(mem.store.events())
    mem.store.close()

    # A "restart": brand-new engine over the same file.
    mem2 = Memory.local(path)
    assert len(mem2.store.events()) == n_events
    assert mem2.answer("where does the user live", subject_id="u1") == "Berlin"
    assert mem2.answer("where does the user live", subject_id="u1", as_of="2020-01-01") == "Delhi"
    # Digests re-derive byte-identically after the storage round-trip.
    deep = mem2.verify_audit(deep=True)
    assert deep.ok, f"deep verify must hold across restart: {deep}"
    mem2.store.close()


def test_sql_tamper_caught_at_exact_seq(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    mem = Memory.local(path)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")

    # The attacker edits a stored fact in place with their own connection.
    attacker = sqlite3.connect(str(path))
    attacker.row_factory = sqlite3.Row
    row = next(
        r for r in attacker.execute("SELECT seq, payload FROM event ORDER BY seq")
        if json.loads(r["payload"]).get("predicate") == "lives_in"
    )
    doc = json.loads(row["payload"])
    doc["object"] = "Pyongyang"
    attacker.execute("UPDATE event SET payload = ? WHERE seq = ?", (json.dumps(doc), row["seq"]))
    attacker.commit()
    attacker.close()

    assert mem.verify_audit().ok  # ledger untouched — chain-only is fooled
    deep = mem.verify_audit(deep=True)
    assert not deep.ok
    assert deep.broken_at == row["seq"]
    mem.store.close()


def test_char_span_tamper_caught(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    mem = Memory.local(path)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1")
    attacker = sqlite3.connect(str(path))
    attacker.row_factory = sqlite3.Row
    row = next(
        r for r in attacker.execute("SELECT seq, payload FROM event ORDER BY seq")
        if json.loads(r["payload"]).get("char_span")
    )
    doc = json.loads(row["payload"])
    doc["char_span"] = [0, 2]  # re-point provenance
    attacker.execute("UPDATE event SET payload = ? WHERE seq = ?", (json.dumps(doc), row["seq"]))
    attacker.commit()
    attacker.close()
    deep = mem.verify_audit(deep=True)
    assert not deep.ok and deep.broken_at == row["seq"]
    mem.store.close()


def test_naive_datetime_round_trip_still_deep_verifies(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "n.db")
    naive = datetime(2023, 1, 1, 9, 0, 0)  # no tzinfo — worst case for round-trips
    store.append(
        FactAsserted(
            fact_id="f-n", subject="u1", predicate="likes", object="tea",
            source_episode_id="ep-x", valid_from=naive, recorded_at=naive,
        )
    )
    from notari.audit import verify

    assert verify(store.events(), store.audit_entries()).ok
    store.close()


# --- crypto: at-rest encryption, sanctioned shred, rogue shred -------------- #

def _crypto_store(path: Path) -> SQLiteEventStore:
    pytest.importorskip("cryptography")
    import base64

    from notari.crypto import EnvelopeCipher, generate_kek

    return SQLiteEventStore(path, cipher=EnvelopeCipher(base64.b64decode(generate_kek())))


def test_encryption_at_rest_and_decrypt_on_read(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    mem = Memory(store=_crypto_store(path))
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1")

    raw = sqlite3.connect(str(path)).execute("SELECT payload FROM event").fetchall()
    blob = " ".join(r[0] for r in raw)
    assert "Delhi" not in blob and "Dana" not in blob  # ciphertext at rest
    assert mem.answer("where does the user live", subject_id="u1") == "Delhi"  # decrypts on read
    mem.store.close()


def test_sanctioned_shred_survives_deep_verify(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    mem = Memory(store=_crypto_store(path))
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2")
    assert mem.verify_audit(deep=True).ok

    cert = mem.forget("u1")
    assert cert.episodes_deleted == 1 and cert.facts_deleted >= 2
    assert mem.search("where does the user live", subject_id="u1") == []
    assert mem.answer("where does the user live", subject_id="u2") == "Chennai"  # u2 untouched
    assert mem.verify_audit().ok
    deep = mem.verify_audit(deep=True)
    assert deep.ok, f"deep verify must survive a sanctioned crypto-shred: {deep}"
    mem.store.close()


def test_rogue_key_deletion_flagged(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    mem = Memory(store=_crypto_store(path))
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2")
    # Destroy the key with NO SubjectForgotten tombstone — an unsanctioned erasure.
    mem.store._keys.shred("u2")
    deep = mem.verify_audit(deep=True)
    assert not deep.ok and deep.broken_at is not None
    mem.store.close()


# --- concurrency ------------------------------------------------------------ #

def test_concurrent_appends_never_fork_the_chain(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "n.db")
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def worker(w: int) -> None:
        for i in range(10):
            store.append(
                FactAsserted(
                    fact_id=f"w{w}-f{i}", subject=f"s{w}", predicate="likes",
                    object=f"thing-{i}", source_episode_id="ep", valid_from=t0,
                )
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))

    entries = store.audit_entries()
    assert len(store.events()) == 40 and len(entries) == 40
    assert [e.seq for e in entries] == list(range(1, 41))  # contiguous, no forks
    from notari.audit import verify, verify_entries

    assert verify_entries(entries).ok
    assert verify(store.events(), store.audit_entries()).ok
    store.close()


def test_two_connections_on_one_file_serialize(tmp_path: Path) -> None:
    path = tmp_path / "n.db"
    a, b = SQLiteEventStore(path), SQLiteEventStore(path)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(6):
        writer = a if i % 2 == 0 else b
        writer.append(
            FactAsserted(
                fact_id=f"f{i}", subject="u1", predicate="likes", object=f"o{i}",
                source_episode_id="ep", valid_from=t0,
            )
        )
    # Either connection sees one valid, contiguous chain.
    from notari.audit import verify

    for store in (a, b):
        assert verify(store.events(), store.audit_entries()).ok
        assert [e.seq for e in store.audit_entries()] == list(range(1, 7))
    a.close()
    b.close()


# --- filesystem + defaults --------------------------------------------------- #

@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_file_permissions_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "n.db"  # parents auto-created
    store = SQLiteEventStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    store.close()


def test_entry_points_default_to_durable_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("fastapi")
    monkeypatch.delenv("NOTARI_DATABASE_URL", raising=False)
    monkeypatch.setenv("NOTARI_SQLITE_PATH", str(tmp_path / "mcp.db"))
    # Keep the test fast: no sentence-transformers model load.
    import notari.embed as embed_mod

    monkeypatch.setattr(embed_mod, "default_embedder", lambda: HashEmbedder())

    from notari.mcp import _memory
    from notari.server import _default_memory

    for factory in (_memory, _default_memory):
        mem = factory()
        assert isinstance(mem.store, SQLiteEventStore)
        assert mem.store.path == tmp_path / "mcp.db"
        mem.store.close()
