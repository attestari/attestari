"""Crypto primitives for crypto-shred deletion (no DB needed)."""

from __future__ import annotations

import base64

import pytest

from notari.crypto import NullCipher, cipher_from_env, generate_kek

pytest.importorskip("cryptography")

from notari.crypto import EnvelopeCipher  # noqa: E402


def _cipher() -> EnvelopeCipher:
    return EnvelopeCipher(base64.b64decode(generate_kek()))


def test_encrypt_decrypt_roundtrip() -> None:
    c = _cipher()
    dek = c.new_dek()
    token = c.encrypt(dek, "I live in Berlin")
    assert token != "I live in Berlin"  # actually ciphertext
    assert c.decrypt(dek, token) == "I live in Berlin"


def test_wrap_unwrap_dek() -> None:
    c = _cipher()
    dek = c.new_dek()
    assert c.unwrap(c.wrap(dek)) == dek


def test_wrong_dek_cannot_decrypt() -> None:
    c = _cipher()
    token = c.encrypt(c.new_dek(), "secret")
    with pytest.raises(Exception):
        c.decrypt(c.new_dek(), token)  # different DEK -> auth failure


def test_destroying_dek_makes_content_unrecoverable() -> None:
    # The crypto-shred guarantee: without the DEK, the ciphertext is just noise.
    c = _cipher()
    dek = c.new_dek()
    token = c.encrypt(dek, "right to be forgotten")
    del dek  # simulate destroying the key
    # A fresh, unrelated key cannot recover it.
    with pytest.raises(Exception):
        c.decrypt(c.new_dek(), token)


def test_null_cipher_is_passthrough() -> None:
    c = NullCipher()
    assert not c.enabled
    assert c.encrypt(b"", "hello") == "hello"
    assert c.decrypt(b"", "hello") == "hello"


def test_cipher_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTARI_KEK", raising=False)
    assert not cipher_from_env().enabled
    monkeypatch.setenv("NOTARI_KEK", generate_kek())
    assert cipher_from_env().enabled
