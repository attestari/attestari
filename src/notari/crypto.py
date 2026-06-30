"""Envelope encryption for crypto-shred deletion.

The deletion story rests on a simple idea: encrypt each subject's PII at rest
with a per-subject **data encryption key (DEK)**, wrap that DEK under a root
**key encryption key (KEK)**, and store only the wrapped DEK. To *forget* a
subject you destroy their wrapped DEK — after which every ciphertext belonging to
them is unrecoverable, even though the (immutable) event rows physically remain.
The audit skeleton survives; the content does not.

Encryption is **opt-in**: with no `NOTARI_KEK` configured we return a `NullCipher`
(passthrough), so the default and zero-dependency paths are byte-for-byte
unchanged. Requires the `crypto` extra (`cryptography`) when enabled.
"""

from __future__ import annotations

import base64
import os
import secrets

_NONCE = 12  # AES-GCM nonce length


class NullCipher:
    """No-op cipher: storage stays plaintext (default when NOTARI_KEK is unset)."""

    enabled = False

    def new_dek(self) -> bytes:
        return b""

    def wrap(self, dek: bytes) -> bytes:
        return b""

    def unwrap(self, wrapped: bytes) -> bytes:
        return b""

    def encrypt(self, dek: bytes, text: str) -> str:
        return text

    def decrypt(self, dek: bytes, token: str) -> str:
        return token


class EnvelopeCipher:
    """AES-256-GCM envelope encryption. DEKs encrypt content; the KEK wraps DEKs."""

    enabled = True

    def __init__(self, kek: bytes) -> None:
        if len(kek) not in (16, 24, 32):
            raise ValueError("KEK must be 16, 24, or 32 bytes (got %d)" % len(kek))
        self._kek = kek

    def new_dek(self) -> bytes:
        return secrets.token_bytes(32)

    def wrap(self, dek: bytes) -> bytes:
        return self._seal(self._kek, dek)

    def unwrap(self, wrapped: bytes) -> bytes:
        return self._open(self._kek, wrapped)

    def encrypt(self, dek: bytes, text: str) -> str:
        return base64.b64encode(self._seal(dek, text.encode())).decode()

    def decrypt(self, dek: bytes, token: str) -> str:
        return self._open(dek, base64.b64decode(token)).decode()

    # --- internals ---

    def _aesgcm(self, key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # lazy

        return AESGCM(key)

    def _seal(self, key: bytes, plaintext: bytes) -> bytes:
        nonce = secrets.token_bytes(_NONCE)
        return nonce + self._aesgcm(key).encrypt(nonce, plaintext, None)

    def _open(self, key: bytes, blob: bytes) -> bytes:
        return self._aesgcm(key).decrypt(blob[:_NONCE], blob[_NONCE:], None)


def cipher_from_env() -> NullCipher | EnvelopeCipher:
    """EnvelopeCipher if NOTARI_KEK (base64) is set, else a NullCipher."""
    kek_b64 = os.environ.get("NOTARI_KEK")
    if not kek_b64:
        return NullCipher()
    return EnvelopeCipher(base64.b64decode(kek_b64))


def generate_kek() -> str:
    """A fresh base64 256-bit KEK (for `export NOTARI_KEK=...`)."""
    return base64.b64encode(secrets.token_bytes(32)).decode()
