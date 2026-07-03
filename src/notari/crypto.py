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

The DEK lifecycle (create → wrap → unwrap → shred) lives in one place — the
`KeyManager` — behind a `Keyring` port for where the wrapped DEKs rest (a dict
in memory, the `keyring` table in Postgres). Both event-store adapters delegate
to it, so "how we seal, encrypt, and shred" cannot drift between the two.
"""

from __future__ import annotations

import base64
import os
import secrets
from typing import Protocol

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


class Keyring(Protocol):
    """Where wrapped DEKs live at rest.

    Adapters: a dict for the in-memory store, the `keyring` table for Postgres.
    """

    def get(self, subject_id: str) -> bytes | None: ...

    def put(self, subject_id: str, wrapped_dek: bytes) -> None: ...

    def delete(self, subject_id: str) -> None: ...

    def all(self) -> dict[str, bytes]: ...


class InMemoryKeyring:
    """Keyring adapter backed by a plain dict (the zero-dependency path)."""

    def __init__(self) -> None:
        self._wrapped: dict[str, bytes] = {}

    def get(self, subject_id: str) -> bytes | None:
        return self._wrapped.get(subject_id)

    def put(self, subject_id: str, wrapped_dek: bytes) -> None:
        self._wrapped[subject_id] = wrapped_dek

    def delete(self, subject_id: str) -> None:
        self._wrapped.pop(subject_id, None)

    def all(self) -> dict[str, bytes]:
        return dict(self._wrapped)

    def __contains__(self, subject_id: str) -> bool:
        return subject_id in self._wrapped


class KeyManager:
    """Per-subject key lifecycle: create/unwrap DEKs, the decrypt map for reads,
    and crypto-shred. This is the single definition of the deletion guarantee —
    the store adapters differ only in where bytes land, never in key semantics.
    """

    def __init__(self, cipher: NullCipher | EnvelopeCipher, keyring: Keyring) -> None:
        self.cipher = cipher
        self.keyring = keyring
        self._dek: dict[str, bytes] = {}  # subject_id -> unwrapped DEK (per-process cache)

    def dek_for(self, subject_id: str) -> bytes:
        """Get (or create + persist) the subject's data key, unwrapped."""
        if subject_id in self._dek:
            return self._dek[subject_id]
        wrapped = self.keyring.get(subject_id)
        if wrapped is not None:
            dek = self.cipher.unwrap(wrapped)
        else:
            dek = self.cipher.new_dek()
            self.keyring.put(subject_id, self.cipher.wrap(dek))
        self._dek[subject_id] = dek
        return dek

    def live_deks(self) -> dict[str, bytes]:
        """All subjects whose DEK is intact (used to decrypt on read)."""
        if not self.cipher.enabled:
            return {}
        return {sid: self.cipher.unwrap(w) for sid, w in self.keyring.all().items()}

    def shred(self, subject_id: str) -> None:
        """Destroy the subject's DEK — their ciphertext becomes unrecoverable."""
        self.keyring.delete(subject_id)
        self._dek.pop(subject_id, None)

    def encrypt_for(self, subject_id: str, text: str) -> str:
        """Encrypt `text` under the subject's DEK (creating the DEK if new)."""
        return self.cipher.encrypt(self.dek_for(subject_id), text)

    def reset_cache(self) -> None:
        """Drop unwrapped-DEK cache (e.g. after a test truncate)."""
        self._dek.clear()


def cipher_from_env() -> NullCipher | EnvelopeCipher:
    """EnvelopeCipher if NOTARI_KEK (base64) is set, else a NullCipher."""
    kek_b64 = os.environ.get("NOTARI_KEK")
    if not kek_b64:
        return NullCipher()
    return EnvelopeCipher(base64.b64decode(kek_b64))


def generate_kek() -> str:
    """A fresh base64 256-bit KEK (for `export NOTARI_KEK=...`)."""
    return base64.b64encode(secrets.token_bytes(32)).decode()
