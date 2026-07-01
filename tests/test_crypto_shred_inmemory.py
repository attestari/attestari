"""Crypto-shred deletion in the zero-dependency in-memory store.

Mirrors the Postgres crypto-shred contract (test_crypto_shred.py) with no
database: an EnvelopeCipher encrypts PII at rest, forget() destroys the subject's
DEK, and the retained ciphertext becomes unrecoverable while the audit proof
survives.
"""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("cryptography")

from notari import EnvelopeCipher, InMemoryEventStore, Memory, generate_kek  # noqa: E402
from notari.events import EpisodeIngested  # noqa: E402


def _encrypting_memory() -> tuple[Memory, InMemoryEventStore]:
    cipher = EnvelopeCipher(base64.b64decode(generate_kek()))
    store = InMemoryEventStore(cipher=cipher)
    return Memory(store=store), store


def _raw_payloads(store: InMemoryEventStore, subject_id: str) -> list[str]:
    return [
        e.payload for e in store._log
        if isinstance(e, EpisodeIngested) and e.scope.subject_id == subject_id
    ]


def test_pii_is_encrypted_at_rest() -> None:
    mem, store = _encrypting_memory()
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    raw = _raw_payloads(store, "u1")
    assert raw and "Delhi" not in raw[0] and "Dana" not in raw[0]  # ciphertext at rest
    # ...but reads decrypt transparently while the key is intact.
    assert mem.answer("where does the user live", subject_id="u1") == "Delhi"


def test_forget_crypto_shreds_and_is_unrecoverable() -> None:
    mem, store = _encrypting_memory()
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2", valid_from="2021-01-01")

    cert = mem.forget("u1", requested_by="dpo@example.com")
    assert cert.facts_deleted >= 1

    # The subject's key is destroyed...
    assert "u1" not in store._keyring
    # ...the ciphertext row physically remains but is unreadable...
    assert _raw_payloads(store, "u1") and "Delhi" not in _raw_payloads(store, "u1")[0]
    # ...so u1 is unrecoverable, while u2 is untouched.
    assert mem.answer("where does the user live", subject_id="u1") is None
    assert mem.timeline(subject_id="u1") == []
    assert mem.answer("where does the user live", subject_id="u2") == "Chennai"

    # events() drops the erased subject entirely.
    assert not any(getattr(e.scope, "subject_id", None) == "u1"
                   for e in store.events() if isinstance(e, EpisodeIngested))

    # The tamper-evident audit chain survives the shred (it hashes digests).
    assert mem.verify_audit().ok


def test_default_store_is_plaintext_and_unchanged() -> None:
    # No cipher -> NullCipher passthrough: the zero-dep default stores plaintext.
    mem = Memory()
    mem.add("I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    raw = [e.payload for e in mem.store._log if isinstance(e, EpisodeIngested)]
    assert any("Delhi" in p for p in raw)
