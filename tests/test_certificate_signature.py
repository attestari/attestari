"""Deletion-certificate signing.

With encryption enabled (a KEK), `forget()` returns a certificate signed with
HMAC-SHA256 under a KEK-derived key; `verify_certificate` recomputes the
canonical payload, so forging a certificate or altering any field of a real one
fails verification. Without a KEK the certificate is honest about it: unsigned.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest

pytest.importorskip("cryptography")

from notari import (  # noqa: E402
    EnvelopeCipher,
    InMemoryEventStore,
    Memory,
    SQLiteEventStore,
    generate_kek,
    verify_certificate,
)
from notari.crypto import CERT_ALGORITHM  # noqa: E402


def _signed_forget(store) -> tuple:
    mem = Memory(store=store)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    return mem.forget("u1", requested_by="dpo@example.com")


def test_certificate_is_signed_with_kek_inmemory() -> None:
    kek = generate_kek()
    cert = _signed_forget(InMemoryEventStore(cipher=EnvelopeCipher(base64.b64decode(kek))))
    assert cert.algorithm == CERT_ALGORITHM
    assert cert.signature  # non-empty base64
    assert verify_certificate(cert, kek)


def test_certificate_is_signed_with_kek_sqlite(tmp_path) -> None:
    kek = generate_kek()
    store = SQLiteEventStore(
        tmp_path / "agent.db", cipher=EnvelopeCipher(base64.b64decode(kek))
    )
    cert = _signed_forget(store)
    assert cert.algorithm == CERT_ALGORITHM
    assert verify_certificate(cert, kek)


def test_altering_any_field_breaks_verification() -> None:
    kek = generate_kek()
    cert = _signed_forget(InMemoryEventStore(cipher=EnvelopeCipher(base64.b64decode(kek))))
    assert verify_certificate(cert, kek)
    for forged in (
        dataclasses.replace(cert, facts_deleted=cert.facts_deleted + 1),
        dataclasses.replace(cert, subject_id="someone-else"),
        dataclasses.replace(cert, manifest_hash="0" * 64),
        dataclasses.replace(cert, requested_by="attacker"),
    ):
        assert not verify_certificate(forged, kek)


def test_wrong_kek_fails_verification() -> None:
    kek = generate_kek()
    cert = _signed_forget(InMemoryEventStore(cipher=EnvelopeCipher(base64.b64decode(kek))))
    assert not verify_certificate(cert, generate_kek())


def test_no_kek_issues_unsigned_certificate() -> None:
    mem = Memory()  # NullCipher: logical delete, no root of trust to sign with
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    cert = mem.forget("u1")
    assert cert.signature is None and cert.algorithm is None
    assert not verify_certificate(cert, generate_kek())
