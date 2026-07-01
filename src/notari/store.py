"""EventStore port + the zero-dependency in-memory adapter.

The store only does two things: append events and hand them back in order. That
minimalism is the point — it's what lets the projection be a pure fold and the
Postgres adapter be a thin swap behind the same protocol.

Encryption is opt-in and mirrors the Postgres adapter: pass an `EnvelopeCipher`
(or set `NOTARI_KEK`) and each subject's PII — episode payloads and fact objects —
is encrypted at rest under a per-subject key. `shred_subject` destroys that key,
after which the retained ciphertext is unrecoverable and the subject's events drop
out of `events()`. The default is a `NullCipher` (passthrough), so the
zero-dependency path is byte-for-byte unchanged.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol, runtime_checkable

from .audit import GENESIS, AuditEntry, digest, link
from .crypto import EnvelopeCipher, NullCipher, cipher_from_env
from .events import EpisodeIngested, Event, FactAsserted


@runtime_checkable
class EventStore(Protocol):
    """A durable, totally-ordered, append-only log of events."""

    def append(self, event: Event) -> None: ...

    def events(self) -> list[Event]:
        """All events in append order. The projection folds this."""
        ...

    def audit_entries(self) -> list[AuditEntry]:
        """The tamper-evident hash chain, in order (for verify_audit)."""
        ...


class InMemoryEventStore:
    """In-memory adapter: keeps the log in a list. Deterministic and dependency
    free, so the spike, tests, and eval run anywhere. The PostgresEventStore
    implements the same protocol against db/schema.sql.

    With a cipher enabled, PII is stored as ciphertext and `forget()` (via
    `shred_subject`) makes it unrecoverable — the same crypto-shred guarantee as
    the durable adapter, with no database.
    """

    def __init__(self, cipher: NullCipher | EnvelopeCipher | None = None) -> None:
        self._log: list[Event] = []
        self._audit: list[AuditEntry] = []
        # Encryption is opt-in: EnvelopeCipher when NOTARI_KEK is set, else NullCipher.
        self.cipher = cipher or cipher_from_env()
        self._keyring: dict[str, bytes] = {}  # subject_id -> wrapped DEK (at rest)
        self._dek: dict[str, bytes] = {}       # subject_id -> unwrapped DEK (cache)

    # --- crypto-shred key management ------------------------------------ #

    def _dek_for(self, subject_id: str) -> bytes:
        """Get (or create) the subject's data key, unwrapped."""
        if subject_id in self._dek:
            return self._dek[subject_id]
        wrapped = self._keyring.get(subject_id)
        dek = self.cipher.unwrap(wrapped) if wrapped is not None else self.cipher.new_dek()
        if wrapped is None:
            self._keyring[subject_id] = self.cipher.wrap(dek)
        self._dek[subject_id] = dek
        return dek

    def _live_deks(self) -> dict[str, bytes]:
        """All subjects whose DEK is intact (used to decrypt on read)."""
        if not self.cipher.enabled:
            return {}
        return {sid: self.cipher.unwrap(w) for sid, w in self._keyring.items()}

    def shred_subject(self, subject_id: str) -> None:
        """Destroy the subject's DEK — their ciphertext becomes unrecoverable."""
        self._keyring.pop(subject_id, None)
        self._dek.pop(subject_id, None)

    # --- write path ----------------------------------------------------- #

    def append(self, event: Event) -> None:
        # Persist ciphertext for PII when encryption is on; the audit chain still
        # commits to the *plaintext* digest (so verification is content-faithful
        # and survives a later shred).
        stored = event
        if self.cipher.enabled:
            if isinstance(event, EpisodeIngested) and event.scope.subject_id:
                stored = dataclasses.replace(
                    event, payload=self.cipher.encrypt(self._dek_for(event.scope.subject_id), event.payload)
                )
            elif isinstance(event, FactAsserted) and event.scope.subject_id:
                stored = dataclasses.replace(
                    event, object=self.cipher.encrypt(self._dek_for(event.scope.subject_id), event.object)
                )
        self._log.append(stored)

        kind, ref, payload_hash = digest(event)  # digest the plaintext event
        prev = self._audit[-1].entry_hash if self._audit else GENESIS
        self._audit.append(
            AuditEntry(
                seq=len(self._audit) + 1,
                kind=kind,
                ref=ref,
                payload_hash=payload_hash,
                prev_hash=prev,
                entry_hash=link(prev, payload_hash),
            )
        )

    def events(self) -> list[Event]:
        if not self.cipher.enabled:
            # Return a copy so callers can't mutate the log out from under us.
            return list(self._log)

        deks = self._live_deks()
        out: list[Event] = []
        readable_episodes: set[str] = set()
        for ev in self._log:
            if isinstance(ev, EpisodeIngested) and ev.scope.subject_id:
                sid = ev.scope.subject_id
                if sid not in deks:
                    continue  # DEK destroyed -> subject erased: episode is unreadable
                readable_episodes.add(ev.episode_id)
                out.append(dataclasses.replace(ev, payload=self.cipher.decrypt(deks[sid], ev.payload)))
            elif isinstance(ev, FactAsserted) and ev.scope.subject_id:
                sid = ev.scope.subject_id
                if sid not in deks:
                    continue  # source subject erased -> the fact is erased too
                out.append(dataclasses.replace(ev, object=self.cipher.decrypt(deks[sid], ev.object)))
            else:
                out.append(ev)
        return out

    def audit_entries(self) -> list[AuditEntry]:
        return list(self._audit)

    def __len__(self) -> int:
        return len(self._log)
