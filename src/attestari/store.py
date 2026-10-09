"""EventStore port + the zero-dependency in-memory adapter.

The store only does two things: append events and hand them back in order. That
minimalism is the point — it's what lets the projection be a pure fold and the
Postgres adapter be a thin swap behind the same protocol.

Encryption is opt-in and mirrors the Postgres adapter: pass an `EnvelopeCipher`
(or set `ATTESTARI_KEK`) and each subject's PII — episode payloads and fact objects —
is encrypted at rest under a per-subject key. `shred_subject` destroys that key,
after which the retained ciphertext is unrecoverable and the subject's events drop
out of `events()`. The default is a `NullCipher` (passthrough), so the
zero-dependency path is byte-for-byte unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .audit import GENESIS, AuditEntry, next_entry
from .crypto import EnvelopeCipher, InMemoryKeyring, KeyManager, NullCipher, cipher_from_env
from .events import EpisodeIngested, Event, FactAsserted, SubjectForgotten


@dataclass(frozen=True)
class LogPosition:
    """A point in the log: an audit entry's seq and hash. The hash commits to
    every entry before it, so a position still on the chain identifies exactly
    which events came before it."""

    seq: int
    entry_hash: str


LOG_START = LogPosition(0, GENESIS)


class ForgottenSubjectError(Exception):
    """A write carried an episode or fact for a subject that `forget()` has
    erased. Nothing was written: a forgotten subject's records stay closed, so
    a person who comes back needs a new subject id."""

    def __init__(self, subject_id: str) -> None:
        super().__init__(
            "this subject was forgotten, so its records accept no new content and "
            "nothing was written; store new data under a new subject id"
        )
        self.subject_id = subject_id


def content_subject(event: Event) -> str | None:
    """The subject whose content an episode or fact carries (its scope's
    subject), or None for every other event. These are the events a forgotten
    subject's records refuse."""
    if isinstance(event, (EpisodeIngested, FactAsserted)):
        return event.scope.subject_id
    return None


@runtime_checkable
class EventStore(Protocol):
    """A durable, totally-ordered, append-only log of events.

    The adapters here also provide `write_lock()`, a re-entrant context manager:
    the appends inside it commit together, and no other writer (thread or
    process) appends in between. `Memory` holds it while it decides supersession
    from the live state and writes the result. A store without one gets a lock
    that only covers the threads of one `Memory`.

    They also refuse an episode or fact for a subject with a `SubjectForgotten`
    tombstone, raising `ForgottenSubjectError`. They check under that lock, so a
    write racing a `forget()` lands before the erasure or is refused after it.

    And they provide `changes_since(position)`: the events appended after a
    `LogPosition`, read the way `events()` reads them, plus the new head. It
    returns None when the position is no longer on the chain (a rolled-back
    write, a truncate), and the reader starts again from `LOG_START`. A
    projection backend uses it to fold only what is new; a store without it
    gets a full fold on every read."""

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
    implements the same protocol against src/attestari/db/schema.sql.

    With a cipher enabled, PII is stored as ciphertext and `forget()` (via
    `shred_subject`) makes it unrecoverable — the same crypto-shred guarantee as
    the durable adapter, with no database.
    """

    def __init__(self, cipher: NullCipher | EnvelopeCipher | None = None) -> None:
        self._log: list[Event] = []
        self._audit: list[AuditEntry] = []
        # Encryption is opt-in: EnvelopeCipher when ATTESTARI_KEK is set, else NullCipher.
        self.cipher = cipher or cipher_from_env()
        # Key lifecycle is delegated to the shared KeyManager; only the resting
        # place of the wrapped DEKs (a dict here, a table in Postgres) differs.
        self._keyring = InMemoryKeyring()
        self._keys = KeyManager(self.cipher, self._keyring)
        # Threads share this list-backed log: appends, key changes and reads
        # take the lock, so the chain can't fork and a new subject can't get
        # two data keys.
        self._lock = threading.RLock()
        self._write_depth = 0
        self._forgotten: set[str] = set()  # subjects with a tombstone on the log

    # --- crypto-shred key management ------------------------------------ #

    def shred_subject(self, subject_id: str) -> None:
        """Destroy the subject's DEK — their ciphertext becomes unrecoverable."""
        with self._lock:
            self._keys.shred(subject_id)

    def commit_keys(self) -> dict[str, bytes]:
        """Commitment keys of the subjects whose DEK is intact (deep verify)."""
        with self._lock:
            return self._keys.commit_keys()

    def erased_refs(self) -> set[str]:
        """Ids of episodes/facts whose content was **sanctioned-erased**: the
        subject's DEK is destroyed AND a `SubjectForgotten` tombstone is on the
        log. Deep audit verification skips exactly these entries; a destroyed
        key with no tombstone is deliberately NOT included, so a rogue key
        deletion fails verification instead of hiding."""
        if not self.cipher.enabled:
            return set()
        with self._lock:
            log = list(self._log)
            tombstoned = {ev.subject_id for ev in log if isinstance(ev, SubjectForgotten)}
            gone = {sid for sid in tombstoned if sid not in self._keyring}
        out: set[str] = set()
        for ev in log:
            if isinstance(ev, EpisodeIngested) and ev.scope.subject_id in gone:
                out.add(ev.episode_id)
            elif isinstance(ev, FactAsserted) and ev.scope.subject_id in gone:
                out.add(ev.fact_id)
        return out

    # --- write path ----------------------------------------------------- #

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """Hold the log for a block of appends. They land together or, if the
        block raises, not at all, as with the database adapters; other threads
        wait. Re-entrant: the outermost block owns the rollback."""
        with self._lock:
            if self._write_depth:
                self._write_depth += 1
                try:
                    yield
                finally:
                    self._write_depth -= 1
                return
            log_n, audit_n = len(self._log), len(self._audit)
            self._write_depth = 1
            try:
                yield
            except BaseException:
                del self._log[log_n:]
                del self._audit[audit_n:]
                self._forgotten = {
                    ev.subject_id for ev in self._log if isinstance(ev, SubjectForgotten)
                }
                raise
            finally:
                self._write_depth = 0

    def append(self, event: Event) -> None:
        # Persist ciphertext for PII when encryption is on; the audit chain
        # commits to the plaintext through keyed commitments (so verification is
        # content-faithful, survives a later shred, and can't confirm guesses
        # after one). See KeyManager.seal.
        with self.write_lock():
            sid = content_subject(event)
            if sid is not None and sid in self._forgotten:
                raise ForgottenSubjectError(sid)
            committed, stored = self._keys.seal(event)
            self._log.append(stored)

            # Chain the committed event's digest (see audit.next_entry — shared
            # with the Postgres adapter, so the two chains cannot diverge).
            prev = self._audit[-1].entry_hash if self._audit else GENESIS
            self._audit.append(next_entry(prev, len(self._audit) + 1, committed))
            if isinstance(event, SubjectForgotten):
                self._forgotten.add(event.subject_id)

    def events(self) -> list[Event]:
        with self._lock:
            return self._keys.open(self._log)

    def changes_since(self, position: LogPosition) -> tuple[LogPosition, list[Event]] | None:
        with self._lock:
            n = len(self._audit)
            if position.seq == 0:
                on_chain = position.entry_hash == GENESIS
            else:
                on_chain = (
                    position.seq <= n
                    and self._audit[position.seq - 1].entry_hash == position.entry_hash
                )
            if not on_chain:
                return None
            head = LogPosition(n, self._audit[-1].entry_hash) if n else LOG_START
            return head, self._keys.open(self._log[position.seq :])

    def audit_entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._audit)

    def __len__(self) -> int:
        with self._lock:
            return len(self._log)
