"""Crypto-shred deletion against a live Postgres.

Skipped unless NOTARI_DATABASE_URL is set and `cryptography` is installed. Sets
NOTARI_KEK for the duration of the test so encryption-at-rest is active.
"""

from __future__ import annotations

import os

import pytest

DSN = os.environ.get("NOTARI_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set NOTARI_DATABASE_URL to run Postgres tests")

pytest.importorskip("cryptography")

from notari import Memory, PostgresEventStore  # noqa: E402
from notari.crypto import generate_kek  # noqa: E402


def test_crypto_shred_makes_subject_unrecoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTARI_KEK", generate_kek())

    PostgresEventStore(DSN).truncate()  # clears tables + keyring (cipher now enabled)
    mem = Memory.postgres(DSN)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2", valid_from="2021-01-01")

    inspector = PostgresEventStore(DSN)

    # PII is encrypted at rest: the raw payload is ciphertext, not the plaintext.
    raw = inspector._conn.execute(
        "SELECT payload FROM episode WHERE subject_id = 'u1'"
    ).fetchone()["payload"]
    assert "Delhi" not in raw and "Dana" not in raw

    # Forget u1 -> crypto-shred (destroy the DEK).
    cert = mem.forget("u1", requested_by="dpo@example.com")
    assert cert.facts_deleted >= 2

    # The subject's key is gone...
    kr = inspector._conn.execute(
        "SELECT count(*) AS n FROM keyring WHERE subject_id = 'u1'"
    ).fetchone()["n"]
    assert kr == 0

    # ...so u1 is unrecoverable even from a brand-new engine; u2 is untouched.
    mem2 = Memory.postgres(DSN)
    assert mem2.answer("where does the user live", subject_id="u1") is None
    assert mem2.answer("where does the user live", subject_id="u2") == "Chennai"
    assert mem2.timeline(subject_id="u1") == []

    # The tamper-evident audit chain survives the crypto-shred (it hashes content
    # digests, not raw content) — you can still prove the log wasn't altered.
    assert mem2.verify_audit().ok
    inspector.close()
