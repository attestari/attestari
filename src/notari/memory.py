"""Memory — the public facade (the Python SDK surface).

Writes events to an `EventStore`; delegates reading (project/search) and
post-write maintenance to a `ProjectionBackend`. The default backend folds the
log in memory; `Memory.postgres(...)` wires the durable Postgres store together
with the materialised-projection backend (pgvector + full-text retrieval).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from .audit import AuditReport, verify, verify_entries
from .backend import InMemoryProjectionBackend, ProjectionBackend
from .embed import Embedder, HashEmbedder
from .events import (
    EntityMerged,
    EntityUnmerged,
    EpisodeIngested,
    FactAsserted,
    FactInvalidated,
    Scope,
    SubjectForgotten,
    utcnow,
)
from .extract import DeterministicExtractor, Extractor
from .predicates import PredicateRegistry, default_registry
from .projection import Edge
from .resolver import EntityResolver, LexicalEntityResolver, ResolutionResult
from .records import DeletionCertificate, Provenance
from .retrieve import SearchResult
from .store import EventStore, InMemoryEventStore


def _coerce_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class Memory:
    def __init__(
        self,
        store: EventStore | None = None,
        extractor: Extractor | None = None,
        embedder: Embedder | None = None,
        backend: ProjectionBackend | None = None,
        predicates: PredicateRegistry | None = None,
        resolver: EntityResolver | None = None,
    ) -> None:
        # NB: `store or ...` would discard a freshly-passed empty store, since
        # InMemoryEventStore defines __len__ (an empty store is falsy). Check None.
        self.store: EventStore = store if store is not None else InMemoryEventStore()
        self.extractor: Extractor = extractor or DeterministicExtractor()
        self.embedder: Embedder = embedder or HashEmbedder()
        self.backend: ProjectionBackend = backend or InMemoryProjectionBackend(
            self.store, self.embedder
        )
        self.predicates: PredicateRegistry = predicates or default_registry()
        self.resolver: EntityResolver = resolver or LexicalEntityResolver(self.embedder)

    @classmethod
    def postgres(
        cls,
        dsn: str | None = None,
        *,
        extractor: Extractor | None = None,
        embedder: Embedder | None = None,
    ) -> "Memory":
        """Durable engine: Postgres event store + materialised pgvector/full-text
        projections. Requires the `postgres` extra."""
        from .store_postgres import PostgresEventStore, PostgresProjectionBackend

        emb = embedder or HashEmbedder()
        store = PostgresEventStore(dsn)
        backend = PostgresProjectionBackend(store, emb)
        return cls(store=store, extractor=extractor, embedder=emb, backend=backend)

    # --- write path ----------------------------------------------------- #

    def add(
        self,
        text: str,
        *,
        subject_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        org_id: str | None = None,
        valid_from: datetime | str | None = None,
        source_ref: str | None = None,
    ) -> list[str]:
        """Ingest a message: store the episode, extract facts, dedup, supersede
        any prior value for the same subject+predicate, append assertions."""
        scope = Scope(subject_id=subject_id, agent_id=agent_id, session_id=session_id, org_id=org_id)
        vfrom = _coerce_dt(valid_from) or utcnow()

        # Canonical (hyphenated) UUID strings: Postgres UUID columns normalise
        # to this form on read, so ids round-trip byte-identically — required
        # for the audit digests (which commit ids) to re-derive exactly.
        episode_id = str(uuid.uuid4())
        self.store.append(
            EpisodeIngested(
                episode_id=episode_id,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                payload=text,
                scope=scope,
                source_ref=source_ref,
            )
        )

        live_edges = self._project().live_edges()
        latest = {(e.subject, e.predicate): (e.fact_id, e.object) for e in live_edges}
        live_triples = {(e.subject, e.predicate, e.object) for e in live_edges}
        seen: set[tuple[str, str, str]] = set()

        asserted: list[str] = []
        for fact in self.extractor.extract(text, scope):
            triple = (fact.subject, fact.predicate, fact.object)
            if triple in seen or triple in live_triples:
                continue  # dedup: this exact fact is already known (in-message or live)
            seen.add(triple)

            key = (fact.subject, fact.predicate)
            single = self.predicates.is_single_valued(fact.predicate)
            if single:
                prior = latest.get(key)
                if prior is not None and prior[1] != fact.object:
                    # Single-valued predicate: a different value supersedes the old.
                    self.store.append(
                        FactInvalidated(fact_id=prior[0], reason="superseded", valid_to=vfrom)
                    )
            # (multi-valued predicates simply coexist)
            fact_id = str(uuid.uuid4())
            self.store.append(
                FactAsserted(
                    fact_id=fact_id,
                    subject=fact.subject,
                    predicate=fact.predicate,
                    object=fact.object,
                    source_episode_id=episode_id,
                    valid_from=vfrom,
                    confidence=fact.confidence,
                    char_span=fact.char_span,
                    scope=scope,
                )
            )
            asserted.append(fact_id)
            if single:
                latest[key] = (fact_id, fact.object)

        self.backend.on_write()
        return asserted

    def merge_entities(self, canonical_id: str, alias_id: str, evidence: str = "manual") -> None:
        """Record that two surface forms are the same entity (entity resolution)."""
        self.store.append(
            EntityMerged(canonical_id=canonical_id, alias_id=alias_id, evidence=evidence)
        )
        self.backend.on_write()

    def unmerge_entities(self, canonical_id: str, alias_id: str) -> None:
        """Reverse a merge — split an alias back out (entity resolution is undoable)."""
        self.store.append(EntityUnmerged(canonical_id=canonical_id, alias_id=alias_id))
        self.backend.on_write()

    def resolve_entities(
        self, names: list[str] | None = None, *, auto: bool = True
    ) -> ResolutionResult:
        """Run entity resolution. With `names=None`, considers all entity names in
        the graph (subjects + objects). Auto-merges high-confidence pairs and
        returns the full result (including the review band for human decision)."""
        if names is None:
            proj = self._project()
            names = sorted(
                {e.subject for e in proj.edges.values()}
                | {e.object for e in proj.edges.values()}
            )
        result = self.resolver.resolve(names)
        if auto:
            for d in result.auto_merges:
                self.merge_entities(d.canonical, d.alias, evidence=f"auto:{d.score}")
        return result

    # --- read path ------------------------------------------------------ #

    def search(
        self,
        query: str,
        *,
        subject_id: str | None = None,
        as_of: datetime | str | None = None,
        limit: int = 5,
    ) -> list[SearchResult]:
        return self.backend.search(
            query, subject_id=subject_id, as_of=_coerce_dt(as_of), limit=limit
        )

    def answer(self, query: str, **kwargs) -> str | None:
        """Convenience: the object of the top-ranked fact (or None)."""
        results = self.search(query, **kwargs)
        return results[0].edge.object if results else None

    def timeline(self, *, subject: str | None = None, subject_id: str | None = None) -> list[Edge]:
        """All facts (current and superseded) for an entity, oldest first."""
        edges = list(self._project().edges.values())
        if subject is not None:
            edges = [e for e in edges if e.subject == subject]
        if subject_id is not None:
            edges = [e for e in edges if e.subject_id == subject_id]
        return sorted(edges, key=lambda e: e.valid_from)

    def get_provenance(self, fact_id: str) -> Provenance | None:
        """Where did this fact come from? Source episode, span, and the exact
        snippet of raw text it was extracted from."""
        asserted = next(
            (e for e in self.store.events() if isinstance(e, FactAsserted) and e.fact_id == fact_id),
            None,
        )
        if asserted is None:
            return None
        episode = next(
            (
                e
                for e in self.store.events()
                if isinstance(e, EpisodeIngested) and e.episode_id == asserted.source_episode_id
            ),
            None,
        )
        snippet = None
        source_ref = None
        if episode is not None:
            source_ref = episode.source_ref
            if asserted.char_span is not None:
                lo, hi = asserted.char_span
                snippet = episode.payload[lo:hi]
        return Provenance(
            fact_id=fact_id,
            source_episode_id=asserted.source_episode_id,
            recorded_at=asserted.recorded_at,
            snippet=snippet,
            source_ref=source_ref,
        )

    def conflicts(self, *, subject_id: str | None = None) -> list[dict]:
        """Surface conflicts: single-valued predicates that held >1 distinct value
        over time. Each group shows the value history and how it was resolved
        (recency). Conflicts are surfaced, not hidden."""
        proj = self._project()
        groups: dict[tuple[str, str], list[Edge]] = {}
        for e in proj.edges.values():
            if subject_id is not None and e.subject_id != subject_id:
                continue
            if not self.predicates.is_single_valued(e.predicate):
                continue
            groups.setdefault((e.subject, e.predicate), []).append(e)

        out: list[dict] = []
        for (subject, predicate), edges in groups.items():
            if len({e.object for e in edges}) < 2:
                continue  # only one value over time -> no conflict
            edges.sort(key=lambda e: e.valid_from)
            out.append(
                {
                    "subject": subject,
                    "predicate": predicate,
                    "resolution": "recency",
                    "values": [
                        {
                            "object": e.object,
                            "valid_from": e.valid_from.isoformat(),
                            "valid_to": e.valid_to.isoformat() if e.valid_to else None,
                            "alive": e.alive,
                        }
                        for e in edges
                    ],
                }
            )
        return out

    # --- deletion ------------------------------------------------------- #

    def forget(self, subject_id: str, *, requested_by: str = "system") -> DeletionCertificate:
        """Right-to-be-forgotten: destroy the subject's lineage and return a
        signed certificate proving it happened."""
        before = self._project()
        episodes = [e for e in before.episodes.values() if e.scope.subject_id == subject_id]
        facts = [e for e in before.edges.values() if e.subject_id == subject_id]

        ids = sorted([e.episode_id for e in episodes] + [e.fact_id for e in facts])
        manifest_hash = hashlib.sha256("\n".join(ids).encode()).hexdigest()

        certificate = DeletionCertificate(
            certificate_id=str(uuid.uuid4()),
            subject_id=subject_id,
            requested_by=requested_by,
            episodes_deleted=len(episodes),
            facts_deleted=len(facts),
            manifest_hash=manifest_hash,
            issued_at=utcnow(),
        )

        self.store.append(SubjectForgotten(subject_id=subject_id, requested_by=requested_by))
        self.backend.on_forget(certificate)
        self.backend.on_write()
        return certificate

    # --- audit ---------------------------------------------------------- #

    def verify_audit(self, *, deep: bool = False) -> AuditReport:
        """Verify the tamper-evident hash chain over the event log.

        `deep=True` also cross-checks that each stored event's content still
        matches the digest committed to the chain (every semantic field,
        including provenance spans and scope) and re-hashes each readable
        episode payload against its `content_hash` — catching silent in-place
        edits, not just ledger tampering. Deep verification **survives
        sanctioned crypto-shreds**: entries whose events were erased with a
        destroyed key *and* a `SubjectForgotten` tombstone are skipped, while a
        destroyed key without a tombstone (a rogue shred) fails at that seq.
        The default (chain-only) needs no event content at all.
        """
        if deep:
            erased_fn = getattr(self.store, "erased_refs", None)
            erased = erased_fn() if callable(erased_fn) else frozenset()
            return verify(self.store.events(), self.store.audit_entries(), erased=erased)
        return verify_entries(self.store.audit_entries())

    # --- internals ------------------------------------------------------ #

    def _project(self):
        return self.backend.project()
