"""Deep audit verification: field coverage, payload check, shred-awareness,
and the Postgres append-order regression (the 2026-07-06 LLD-validation gaps).

Gap 1 — Postgres deep verify must hold on interleaved logs (event_seq order).
Gap 2 — digest() commits every semantic field (spans, confidence, scope, ...).
Gap 3 — a payload edited in place (stored content_hash kept) is caught.
Gap 4 — deep verify survives a sanctioned crypto-shred (key destroyed + tombstone)
        and fails on a rogue shred (key destroyed, no tombstone).
"""

from __future__ import annotations

import base64
import dataclasses
import os

import pytest

from notari import Memory
from notari.events import EpisodeIngested, FactAsserted


def _seed() -> Memory:
    mem = Memory()
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I moved to Berlin.", subject_id="u1", valid_from="2026-03-01")
    return mem


def _tamper_first_fact(mem: Memory, **changes) -> int:
    log = mem.store._log
    idx = next(i for i, e in enumerate(log) if isinstance(e, FactAsserted))
    log[idx] = dataclasses.replace(log[idx], **changes)
    return idx


# --- gap 2: every semantic field is committed ------------------------------ #

@pytest.mark.parametrize(
    "field",
    ["char_span", "confidence", "valid_to", "recorded_at"],
)
def test_deep_verify_catches_field_tamper(field: str) -> None:
    from notari.events import utcnow

    mem = _seed()
    changes = {
        "char_span": {"char_span": (0, 4)},          # re-point provenance
        "confidence": {"confidence": 0.5},           # weaken/strengthen a fact
        "valid_to": {"valid_to": utcnow()},          # close a validity window
        "recorded_at": {"recorded_at": utcnow()},    # re-date the record
    }[field]
    idx = _tamper_first_fact(mem, **changes)
    assert mem.verify_audit().ok  # chain-only can't see content
    deep = mem.verify_audit(deep=True)
    assert not deep.ok
    assert deep.broken_at == idx + 1


def test_deep_verify_catches_scope_retag() -> None:
    from notari.events import Scope

    mem = _seed()
    idx = _tamper_first_fact(mem, scope=Scope(subject_id="mallory"))
    deep = mem.verify_audit(deep=True)
    assert not deep.ok and deep.broken_at == idx + 1


# --- gap 3: payload re-hashed against content_hash ------------------------- #

def test_deep_verify_catches_payload_edit_keeping_content_hash() -> None:
    mem = _seed()
    log = mem.store._log
    idx = next(i for i, e in enumerate(log) if isinstance(e, EpisodeIngested))
    # Edit the raw payload but keep the stored content_hash — the digest alone
    # would still match, so this is exactly what the re-hash check must catch.
    log[idx] = dataclasses.replace(log[idx], payload="My name is Mallory.")
    deep = mem.verify_audit(deep=True)
    assert not deep.ok
    assert deep.broken_at == idx + 1


# --- gap 4: sanctioned shreds survive, rogue shreds fail -------------------- #

def _crypto_mem() -> Memory:
    pytest.importorskip("cryptography")
    from notari.crypto import EnvelopeCipher, generate_kek
    from notari.store import InMemoryEventStore

    cipher = EnvelopeCipher(base64.b64decode(generate_kek()))
    store = InMemoryEventStore(cipher=cipher)
    mem = Memory(store=store)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2", valid_from="2021-01-01")
    return mem


def test_deep_verify_survives_sanctioned_shred() -> None:
    mem = _crypto_mem()
    assert mem.verify_audit(deep=True).ok
    mem.forget("u1")  # tombstone + key destruction
    deep = mem.verify_audit(deep=True)
    assert deep.ok, f"deep verify must survive a sanctioned crypto-shred: {deep}"
    # And the other subject's content is still fully verified.
    assert mem.verify_audit().ok


def test_deep_verify_flags_rogue_key_deletion() -> None:
    mem = _crypto_mem()
    # Destroy u2's key with NO SubjectForgotten tombstone — an unsanctioned
    # erasure. Reads go dark, but deep verification must scream, not shrug.
    mem.store._keys.shred("u2")
    deep = mem.verify_audit(deep=True)
    assert not deep.ok
    assert deep.broken_at is not None


# --- gap 1: Postgres append-order regression -------------------------------- #

DSN = os.environ.get("NOTARI_DATABASE_URL")
pg = pytest.mark.skipif(not DSN, reason="set NOTARI_DATABASE_URL to run Postgres tests")


def _pg_reset() -> None:
    from notari import PostgresEventStore

    store = PostgresEventStore(DSN)
    store.truncate()
    store.close()


@pg
def test_postgres_deep_verify_holds_on_interleaved_log() -> None:
    _pg_reset()
    mem = Memory.postgres(DSN)
    mem.add("Hi, I'm Alice. I live in Berlin.", subject_id="u1")
    mem.add("I work at Acme.", subject_id="u1")
    mem.add("Hi, I'm Bob. I use Rust.", subject_id="u2")
    deep = mem.verify_audit(deep=True)
    assert deep.ok, f"clean interleaved log must deep-verify: {deep}"


@pg
def test_postgres_deep_verify_catches_sql_tamper() -> None:
    _pg_reset()
    mem = Memory.postgres(DSN)
    mem.add("Hi, I'm Alice. I live in Berlin.", subject_id="u1")
    mem.add("I work at Acme.", subject_id="u1")
    mem.store._conn.execute(
        "UPDATE fact_event SET object = 'Pyongyang' WHERE op = 'asserted' AND object = 'Berlin'"
    )
    assert mem.verify_audit().ok  # ledger untouched — chain-only is fooled
    deep = mem.verify_audit(deep=True)
    assert not deep.ok and deep.broken_at is not None


@pg
def test_postgres_deep_verify_survives_shred(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("cryptography")
    from notari.crypto import generate_kek

    monkeypatch.setenv("NOTARI_KEK", generate_kek())
    _pg_reset()
    mem = Memory.postgres(DSN)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2", valid_from="2021-01-01")
    assert mem.verify_audit(deep=True).ok
    mem.forget("u1")
    deep = mem.verify_audit(deep=True)
    assert deep.ok, f"deep verify must survive a sanctioned crypto-shred on Postgres: {deep}"
    _pg_reset()
