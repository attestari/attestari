"""ProjectionBackend — the read/query side, behind a port.

`Memory` writes events to the `EventStore` and delegates all *reading* (project,
search) and post-write maintenance (on_write, on_forget) to a ProjectionBackend.
This is what lets the same engine either fold projections in memory (the default)
or materialise them in Postgres with pgvector/full-text retrieval — without the
facade knowing which.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .embed import Embedder
from .projection import Projection, Projector
from .records import DeletionCertificate
from .retrieve import SearchResult
from .retrieve import search as inmem_search
from .store import EventStore


@runtime_checkable
class ProjectionBackend(Protocol):
    def project(self) -> Projection:
        """Full current state (folded from the log) — for timeline/supersession."""
        ...

    def search(
        self,
        query: str,
        *,
        subject_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> list[SearchResult]: ...

    def on_write(self) -> None:
        """Called after events are appended (refresh materialised state)."""
        ...

    def on_forget(self, certificate: DeletionCertificate) -> None:
        """Called when a subject is forgotten (persist the certificate, etc.)."""
        ...


class InMemoryProjectionBackend:
    """Default backend: fold the event log on every read. Zero dependencies and
    fully deterministic — the reference behaviour every other backend matches."""

    def __init__(self, store: EventStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder
        self._projector = Projector(embedder)

    def project(self) -> Projection:
        return self._projector.build(self.store.events())

    def search(
        self,
        query: str,
        *,
        subject_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> list[SearchResult]:
        return inmem_search(
            self.project(), query, self.embedder, subject_id=subject_id, as_of=as_of, limit=limit
        )

    def on_write(self) -> None:  # nothing to materialise
        pass

    def on_forget(self, certificate: DeletionCertificate) -> None:
        # The fold already purges the subject logically (via the SubjectForgotten
        # tombstone). If the store supports crypto-shred, also destroy the DEK so
        # the subject's ciphertext at rest becomes unrecoverable.
        shred = getattr(self.store, "shred_subject", None)
        if shred is not None:
            shred(certificate.subject_id)
