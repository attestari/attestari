"""EventStore port + the zero-dependency in-memory adapter.

The store only does two things: append events and hand them back in order. That
minimalism is the point — it's what lets the projection be a pure fold and the
Postgres adapter be a thin swap behind the same protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .audit import GENESIS, AuditEntry, digest, link
from .events import Event


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
    implements the same protocol against db/schema.sql."""

    def __init__(self) -> None:
        self._log: list[Event] = []
        self._audit: list[AuditEntry] = []

    def append(self, event: Event) -> None:
        self._log.append(event)
        kind, ref, payload_hash = digest(event)
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
        # Return a copy so callers can't mutate the log out from under us.
        return list(self._log)

    def audit_entries(self) -> list[AuditEntry]:
        return list(self._audit)

    def __len__(self) -> int:
        return len(self._log)
