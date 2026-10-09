"""Keyed commitments: after a shred, the hashes left behind can't confirm a guess.

Up to 0.0.6 an encrypted episode was committed as sha256(payload), and a fact's
object as sha256(object) inside its audit digest. Both are unkeyed, so after
forget() anyone holding the log could hash candidate values and see which one
matched a retained hash. With encryption on, both are now HMACs under a key
derived from the subject's DEK, destroyed with it (KeyManager.seal).

The 0.0.6 fixture (fixtures/sqlite-log-0.0.6.sql) was written by the released
0.0.6 code: `git archive 21d24e0 src`, then Memory.local() with the fixture KEK
below, three messages for u1/u2, forget("u2"), and sqlite3's iterdump().
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from attestari import EnvelopeCipher, InMemoryEventStore, Memory, generate_kek  # noqa: E402
from attestari.audit import digest, verify  # noqa: E402
from attestari.crypto import COMMIT_PREFIX, NullCipher  # noqa: E402
from attestari.events import EpisodeIngested, Event, FactAsserted  # noqa: E402
from attestari.store_sqlite import SQLiteEventStore  # noqa: E402

SENTENCE = "Hi, I'm Dana. I live in Berlin."
CITIES = ["Paris", "London", "Berlin", "Madrid", "Rome", "Delhi"]

FIXTURE = Path(__file__).parent / "fixtures" / "sqlite-log-0.0.6.sql"
FIXTURE_KEK = base64.b64encode(b"attestari-test-kek-0.0.6-fixture").decode()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _encrypting(tier: str, tmp_path: Path) -> tuple[Memory, InMemoryEventStore | SQLiteEventStore]:
    cipher = EnvelopeCipher(base64.b64decode(generate_kek()))
    if tier == "memory":
        store = InMemoryEventStore(cipher=cipher)
    else:
        store = SQLiteEventStore(tmp_path / "log.db", cipher=cipher)
    return Memory(store=store), store


def _retained(store) -> list[Event]:
    """What rests in the log: ciphertext for live and shredded subjects alike."""
    if isinstance(store, InMemoryEventStore):
        return list(store._log)
    return store._raw_events()


def _lives_in(events: list[Event]) -> FactAsserted:
    return next(e for e in events if isinstance(e, FactAsserted) and e.predicate == "lives_in")


@pytest.mark.parametrize("tier", ["memory", "sqlite"])
def test_encrypted_content_is_committed_with_keyed_hashes(tier: str, tmp_path: Path) -> None:
    mem, store = _encrypting(tier, tmp_path)
    mem.add(SENTENCE, subject_id="u1")

    episode = next(e for e in _retained(store) if isinstance(e, EpisodeIngested))
    assert episode.content_hash.startswith(COMMIT_PREFIX)
    assert episode.content_hash != _sha(SENTENCE)
    fact = _lives_in(_retained(store))
    assert fact.object_hash is not None and fact.object_hash.startswith(COMMIT_PREFIX)
    assert mem.verify_audit(deep=True).ok


def test_unencrypted_commitments_are_unchanged() -> None:
    # No KEK: the content is plaintext in the log anyway, and digests stay
    # byte-identical to earlier versions.
    store = InMemoryEventStore(cipher=NullCipher())
    Memory(store=store).add(SENTENCE, subject_id="u1")

    episode = next(e for e in store._log if isinstance(e, EpisodeIngested))
    assert episode.content_hash == _sha(SENTENCE)
    assert all(e.object_hash is None for e in store._log if isinstance(e, FactAsserted))


@pytest.mark.parametrize("tier", ["memory", "sqlite"])
def test_shredded_hashes_do_not_confirm_a_guess(tier: str, tmp_path: Path) -> None:
    # Control: on unkeyed commitments the attack works. Rebuild a fact's digest
    # from its readable fields plus each candidate value, and compare it to
    # the chain. (Harmless here, since the object is plaintext anyway; it
    # shows the method below would catch a leak.)
    plain = InMemoryEventStore(cipher=NullCipher())
    Memory(store=plain).add(SENTENCE, subject_id="u1")
    chain = {e.ref: e.payload_hash for e in plain.audit_entries()}
    fact = _lives_in(plain._log)
    hits = [c for c in CITIES if digest(dataclasses.replace(fact, object=c))[2] == chain[fact.fact_id]]
    assert hits == ["Berlin"]

    # The same attack after a shred of encrypted content finds nothing.
    mem, store = _encrypting(tier, tmp_path)
    mem.add(SENTENCE, subject_id="u1")
    mem.forget("u1")
    retained = _retained(store)
    chain = {e.ref: e.payload_hash for e in store.audit_entries()}

    fact = _lives_in(retained)
    for city in CITIES:
        guess = dataclasses.replace(fact, object=city, object_hash=None)
        assert digest(guess)[2] != chain[fact.fact_id], city

    episode = next(e for e in retained if isinstance(e, EpisodeIngested))
    assert episode.content_hash != _sha(SENTENCE)
    guess = dataclasses.replace(episode, content_hash=_sha(SENTENCE))
    assert digest(guess)[2] != chain[episode.episode_id]

    assert mem.verify_audit(deep=True).ok  # the erasure itself still verifies


@pytest.mark.parametrize("field", ["payload", "object"])
def test_deep_verify_catches_an_in_place_edit_of_sealed_content(field: str) -> None:
    # Re-encrypt different content under the subject's own key, leaving the
    # commitment alone: the digest still matches the chain, so only the
    # keyed re-check can catch it.
    mem, store = _encrypting("memory", Path())
    mem.add(SENTENCE, subject_id="u1")
    cls = EpisodeIngested if field == "payload" else FactAsserted
    idx = next(i for i, e in enumerate(store._log) if isinstance(e, cls))
    forged = store._keys.encrypt_for("u1", "Pyongyang")
    store._log[idx] = dataclasses.replace(store._log[idx], **{field: forged})

    assert mem.verify_audit().ok  # the ledger is untouched
    deep = mem.verify_audit(deep=True)
    assert not deep.ok and deep.broken_at == idx + 1


def test_sqlite_deep_verify_catches_an_edited_encrypted_object(tmp_path: Path) -> None:
    mem, store = _encrypting("sqlite", tmp_path)
    mem.add(SENTENCE, subject_id="u1")
    row = store._conn.execute(
        "SELECT seq, payload FROM event WHERE kind = 'fact_asserted' ORDER BY seq LIMIT 1"
    ).fetchone()
    data = json.loads(row["payload"])
    data["object"] = store._keys.encrypt_for("u1", "Pyongyang")
    store._conn.execute("UPDATE event SET payload = ? WHERE seq = ?", (json.dumps(data), row["seq"]))

    deep = mem.verify_audit(deep=True)
    assert not deep.ok and deep.broken_at == row["seq"]


def test_keyed_commitments_fail_closed_without_their_key() -> None:
    mem, store = _encrypting("memory", Path())
    mem.add(SENTENCE, subject_id="u1")
    events, entries = store.events(), store.audit_entries()
    assert verify(events, entries, commit_keys=store.commit_keys()).ok
    assert not verify(events, entries).ok  # can't re-derive -> not vouched for


def test_a_log_written_by_0_0_6_still_verifies_and_keeps_growing(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(FIXTURE.read_text())
    con.close()
    store = SQLiteEventStore(db, cipher=EnvelopeCipher(base64.b64decode(FIXTURE_KEK)))
    mem = Memory(store=store)

    # Unkeyed 0.0.6 entries, including a 0.0.6-era erasure, verify as before.
    assert mem.verify_audit(deep=True).ok
    assert mem.answer("where does the user live", subject_id="u1") == "Berlin"
    assert mem.answer("where does the user live", subject_id="u2") is None

    # New entries are keyed and chain on after the old ones.
    mem.add("I moved to Paris.", subject_id="u1", valid_from="2026-09-01")
    episodes = [e for e in store._raw_events() if isinstance(e, EpisodeIngested)]
    assert not episodes[0].content_hash.startswith(COMMIT_PREFIX)
    assert episodes[-1].content_hash.startswith(COMMIT_PREFIX)
    assert mem.verify_audit(deep=True).ok
    assert mem.answer("where does the user live", subject_id="u1") == "Paris"

    mem.forget("u1")
    assert mem.verify_audit(deep=True).ok
