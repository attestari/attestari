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

from .audit import GENESIS, AuditEntry, next_entry
from .crypto import EnvelopeCipher, InMemoryKeyring, KeyManager, NullCipher, cipher_from_env
from .events import EpisodeIngested, Event, FactAsserted, SubjectForgotten


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
        # Key lifecycle is delegated to the shared KeyManager; only the resting
        # place of the wrapped DEKs (a dict here, a table in Postgres) differs.
        self._keyring = InMemoryKeyring()
        self._keys = KeyManager(self.cipher, self._keyring)

    # --- crypto-shred key management ------------------------------------ #

    def shred_subject(self, subject_id: str) -> None:
        """Destroy the subject's DEK — their ciphertext becomes unrecoverable."""
        self._keys.shred(subject_id)

    def erased_refs(self) -> set[str]:
        """Ids of episodes/facts whose content was **sanctioned-erased**: the
        subject's DEK is destroyed AND a `SubjectForgotten` tombstone is on the
        log. Deep audit verification skips exactly these entries; a destroyed
        key with no tombstone is deliberately NOT included, so a rogue key
        deletion fails verification instead of hiding."""
        if not self.cipher.enabled:
            return set()
        tombstoned = {ev.subject_id for ev in self._log if isinstance(ev, SubjectForgotten)}
        gone = {sid for sid in tombstoned if sid not in self._keyring}
        out: set[str] = set()
        for ev in self._log:
            if isinstance(ev, EpisodeIngested) and ev.scope.subject_id in gone:
                out.add(ev.episode_id)
            elif isinstance(ev, FactAsserted) and ev.scope.subject_id in gone:
                out.add(ev.fact_id)
        return out

    # --- write path ----------------------------------------------------- #

    def append(self, event: Event) -> None:
        # Persist ciphertext for PII when encryption is on; the audit chain still
        # commits to the *plaintext* digest (so verification is content-faithful
        # and survives a later shred).
        stored = event
        if self.cipher.enabled:
            if isinstance(event, EpisodeIngested) and event.scope.subject_id:
                stored = dataclasses.replace(
                    event, payload=self._keys.encrypt_for(event.scope.subject_id, event.payload)
                )
            elif isinstance(event, FactAsserted) and event.scope.subject_id:
                stored = dataclasses.replace(
                    event, object=self._keys.encrypt_for(event.scope.subject_id, event.object)
                )
        self._log.append(stored)

        # Chain the plaintext event's digest (see audit.next_entry — shared with
        # the Postgres adapter, so the two chains cannot diverge).
        prev = self._audit[-1].entry_hash if self._audit else GENESIS
        self._audit.append(next_entry(prev, len(self._audit) + 1, event))

    def events(self) -> list[Event]:
        if not self.cipher.enabled:
            # Return a copy so callers can't mutate the log out from under us.
            return list(self._log)

        deks = self._keys.live_deks()
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
