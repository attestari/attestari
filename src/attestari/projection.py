"""The projection — a pure fold of the event log into queryable state.

This is the CQRS read model: throw it away and rebuild it from the log at any
time. It materialises the bi-temporal knowledge graph (entities + edges), applies
corrections (supersession), resolves merged entities, and enacts deletion: a
`forget` drops the subject's whole lineage, and anything recorded for them after.

The fold is incremental. `Projector.apply` folds new events into an existing
projection and returns a new one, and folding a log in pieces gives exactly the
projection that folding it whole does, so a reader only has to fold what was
appended since it last looked.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .embed import Embedder
from .events import (
    EntityMerged,
    EntityUnmerged,
    EpisodeIngested,
    Event,
    FactAsserted,
    FactInvalidated,
    SubjectForgotten,
)


@dataclass(frozen=True)
class Edge:
    """A materialised fact: a temporal knowledge-graph edge with provenance.

    Frozen: a projection shares its edges with every caller it hands them to,
    so an edge is replaced when its fact changes, never edited."""

    fact_id: str
    subject: str
    predicate: str
    object: str
    valid_from: datetime
    valid_to: datetime | None
    tx_from: datetime
    tx_to: datetime | None
    confidence: float
    source_episode_id: str
    char_span: tuple[int, int] | None
    subject_id: str | None
    alive: bool
    embedding: list[float] = field(default_factory=list)

    def text(self) -> str:
        return f"{self.subject} {self.predicate} {self.object}"

    def valid_at(self, instant: datetime) -> bool:
        """True if this fact holds in the *world* at `instant` (valid-time)."""
        if instant < self.valid_from:
            return False
        return self.valid_to is None or instant < self.valid_to


@dataclass
class Entity:
    canonical_id: str
    aliases: set[str] = field(default_factory=set)


@dataclass
class Projection:
    episodes: dict[str, EpisodeIngested]
    edges: dict[str, Edge]
    entities: dict[str, Entity]
    alias_of: dict[str, str]
    forgotten: set[str]

    # --- queries -------------------------------------------------------- #

    def live_edges(self, subject_id: str | None = None) -> list[Edge]:
        out = [e for e in self.edges.values() if e.alive]
        if subject_id is not None:
            out = [e for e in out if e.subject_id == subject_id]
        return out

    def edges_asof(self, instant: datetime, subject_id: str | None = None) -> list[Edge]:
        """Facts true in the world at `instant` (bi-temporal valid-time slice)."""
        out = [e for e in self.edges.values() if e.valid_at(instant)]
        if subject_id is not None:
            out = [e for e in out if e.subject_id == subject_id]
        return out

    def resolve(self, entity_id: str) -> str:
        """Follow alias chains to the canonical id."""
        seen: set[str] = set()
        cur = entity_id
        while cur in self.alias_of and cur not in seen:
            seen.add(cur)
            cur = self.alias_of[cur]
        return cur


class Projector:
    """Folds events into a Projection. Pure function of (events, embedder).

    Embeddings are memoized by fact text: a Projector is bound to one embedder,
    so the cache can never go stale, and rebuild-on-write stops re-embedding the
    whole graph on every write — the difference between microseconds and a full
    model pass per rebuild once real embeddings are installed."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self._embed_cache: dict[str, list[float]] = {}

    def _embedding(self, text: str) -> list[float]:
        vec = self._embed_cache.get(text)
        if vec is None:
            vec = self._embed_cache[text] = self.embedder.embed(text)
        return vec

    def empty(self) -> Projection:
        return Projection(episodes={}, edges={}, entities={}, alias_of={}, forgotten=set())

    def build(self, events: Iterable[Event]) -> Projection:
        return self.apply(self.empty(), events)

    def apply(self, base: Projection, events: Iterable[Event]) -> Projection:
        """`base` with `events` folded in, as a new projection.

        `base` is left as it was: its dicts are copied once per call, and an
        edge or entity that changes is replaced rather than edited, so a
        projection never changes after it is returned. Folding a log in pieces
        gives the same projection as folding it whole."""
        events = list(events)
        if not events:
            return base
        episodes = dict(base.episodes)
        edges = dict(base.edges)
        entities = dict(base.entities)
        alias_of = dict(base.alias_of)
        forgotten = set(base.forgotten)

        for ev in events:
            if isinstance(ev, EpisodeIngested):
                if ev.scope.subject_id not in forgotten:
                    episodes[ev.episode_id] = ev

            elif isinstance(ev, FactAsserted):
                if ev.scope.subject_id not in forgotten:
                    edges[ev.fact_id] = Edge(
                        fact_id=ev.fact_id,
                        subject=ev.subject,
                        predicate=ev.predicate,
                        object=ev.object,
                        valid_from=ev.valid_from,
                        valid_to=ev.valid_to,
                        tx_from=ev.recorded_at,
                        tx_to=None,
                        confidence=ev.confidence,
                        source_episode_id=ev.source_episode_id,
                        char_span=ev.char_span,
                        subject_id=ev.scope.subject_id,
                        alive=ev.valid_to is None,
                        embedding=self._embedding(f"{ev.subject} {ev.predicate} {ev.object}"),
                    )
                if ev.subject not in forgotten:
                    entities.setdefault(ev.subject, Entity(canonical_id=ev.subject))

            elif isinstance(ev, FactInvalidated):
                edge = edges.get(ev.fact_id)
                if edge is not None:
                    edges[ev.fact_id] = dataclasses.replace(
                        edge,
                        valid_to=ev.valid_to,
                        tx_to=ev.recorded_at,  # close the system-time record
                        alive=False,
                    )

            elif isinstance(ev, EntityMerged):
                alias_of[ev.alias_id] = ev.canonical_id
                if ev.canonical_id not in forgotten:
                    canon = entities.get(ev.canonical_id)
                    entities[ev.canonical_id] = Entity(
                        canonical_id=ev.canonical_id,
                        aliases=(canon.aliases if canon else set()) | {ev.alias_id},
                    )

            elif isinstance(ev, EntityUnmerged):
                if alias_of.get(ev.alias_id) == ev.canonical_id:
                    del alias_of[ev.alias_id]
                canon = entities.get(ev.canonical_id)
                if canon is not None:
                    entities[ev.canonical_id] = Entity(
                        canonical_id=ev.canonical_id, aliases=canon.aliases - {ev.alias_id}
                    )

            elif isinstance(ev, SubjectForgotten):
                # Enact deletion: drop the subject's entire lineage now, and
                # skip anything recorded for them later (above). There is
                # nothing left to retrieve. (Production additionally destroys
                # the per-subject encryption key so backups are covered.)
                sid = ev.subject_id
                forgotten.add(sid)
                episodes = {
                    eid: ep for eid, ep in episodes.items() if ep.scope.subject_id != sid
                }
                dropped = [e for e in edges.values() if e.subject_id == sid]
                if dropped:
                    edges = {fid: e for fid, e in edges.items() if e.subject_id != sid}
                    self._forget_texts(dropped, edges)
                entities.pop(sid, None)

        return Projection(
            episodes=episodes,
            edges=edges,
            entities=entities,
            alias_of=alias_of,
            forgotten=forgotten,
        )

    def _forget_texts(self, dropped: list[Edge], remaining: dict[str, Edge]) -> None:
        # The memo is keyed by fact text, so it would keep a forgotten subject's
        # facts in this process for as long as it runs. Drop them, unless a fact
        # that remains has the same text.
        texts = {e.text() for e in dropped}
        texts -= {e.text() for e in remaining.values()}
        for text in texts:
            self._embed_cache.pop(text, None)
