"""ProjectionBackend — the read/query side, behind a port.

`Memory` writes events to the `EventStore` and delegates all *reading* (project,
search) and post-write maintenance (on_write, on_forget) to a ProjectionBackend.
This is what lets the same engine either fold projections in memory (the default)
or materialise them in Postgres with pgvector/full-text retrieval — without the
facade knowing which. Both keep their last projection in a `ProjectionCache` and
fold in only the events appended since.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol, runtime_checkable

from .embed import Embedder
from .projection import Projection, Projector
from .records import DeletionCertificate
from .retrieve import SearchResult
from .retrieve import search as inmem_search
from .store import LOG_START, EventStore


class ProjectionCache:
    """The last projection this process folded, and the log position it
    reflects.

    `current()` asks the store for the events appended since that position and
    folds in only those. The store checks the position against the audit
    chain: if it is no longer there (a rolled-back write, a truncate), the
    cache starts again from an empty projection. A store without
    `changes_since` gets a full fold on every call.

    It locks with the store's own lock where there is one, so catching up and
    writing never wait on each other in opposite orders. Projections are never
    changed once returned, so callers read them without a lock."""

    def __init__(self, store: EventStore, projector: Projector) -> None:
        self.store = store
        self.projector = projector
        self._lock = getattr(store, "_lock", None) or threading.RLock()
        self._projection = projector.empty()
        self._position = LOG_START

    def current(self) -> Projection:
        changes_since = getattr(self.store, "changes_since", None)
        if changes_since is None:
            return self.projector.build(self.store.events())
        with self._lock:
            base, changes = self._projection, changes_since(self._position)
            if changes is None:
                base, changes = self.projector.empty(), changes_since(LOG_START)
            position, events = changes
            # Both move only once the fold succeeds, so a failure (an embedder
            # error, say) leaves the cache where it was.
            self._projection = self.projector.apply(base, events)
            self._position = position
            return self._projection


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
        """Called after events are appended, still inside the store's write
        lock, so materialised state can commit with the events it reflects."""
        ...

    def on_forget(self, certificate: DeletionCertificate) -> None:
        """Called when a subject is forgotten (persist the certificate, etc.)."""
        ...


class InMemoryProjectionBackend:
    """Default backend: the projection held in this process, kept current by
    folding in what each read finds new. Zero dependencies and fully
    deterministic — the reference behaviour every other backend matches."""

    def __init__(self, store: EventStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder
        self._projector = Projector(embedder)
        self._cache = ProjectionCache(store, self._projector)

    def project(self) -> Projection:
        return self._cache.current()

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
