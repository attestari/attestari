"""The projection — a pure fold of the event log into queryable state.

This is the CQRS read model: throw it away and rebuild it from the log at any
time. It materialises the bi-temporal knowledge graph (entities + edges), applies
corrections (supersession), resolves merged entities, and enacts deletion. A
`forget` is honoured here simply by rebuilding without the subject's lineage.
"""

from __future__ import annotations

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


@dataclass
class Edge:
    """A materialised fact: a temporal knowledge-graph edge with provenance."""

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

    def build(self, events: list[Event]) -> Projection:
        episodes: dict[str, EpisodeIngested] = {}
        edges: dict[str, Edge] = {}
        entities: dict[str, Entity] = {}
        alias_of: dict[str, str] = {}
        forgotten: set[str] = set()

        for ev in events:
            if isinstance(ev, EpisodeIngested):
                episodes[ev.episode_id] = ev

            elif isinstance(ev, FactAsserted):
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
                entities.setdefault(ev.subject, Entity(canonical_id=ev.subject))

            elif isinstance(ev, FactInvalidated):
                edge = edges.get(ev.fact_id)
                if edge is not None:
                    edge.valid_to = ev.valid_to
                    edge.tx_to = ev.recorded_at  # close the system-time record
                    edge.alive = False

            elif isinstance(ev, EntityMerged):
                alias_of[ev.alias_id] = ev.canonical_id
                canon = entities.setdefault(ev.canonical_id, Entity(canonical_id=ev.canonical_id))
                canon.aliases.add(ev.alias_id)

            elif isinstance(ev, EntityUnmerged):
                if alias_of.get(ev.alias_id) == ev.canonical_id:
                    del alias_of[ev.alias_id]
                canon = entities.get(ev.canonical_id)
                if canon is not None:
                    canon.aliases.discard(ev.alias_id)

            elif isinstance(ev, SubjectForgotten):
                forgotten.add(ev.subject_id)

        # Enact deletion: drop the forgotten subjects' entire lineage. Because
        # the projection is rebuilt from the log, this genuinely removes them —
        # there is nothing left to retrieve. (Production additionally destroys
        # the per-subject encryption key so backups are covered.)
        if forgotten:
            episodes = {
                eid: ep for eid, ep in episodes.items() if ep.scope.subject_id not in forgotten
            }
            edges = {fid: e for fid, e in edges.items() if e.subject_id not in forgotten}
            entities = {cid: ent for cid, ent in entities.items() if cid not in forgotten}

        return Projection(
            episodes=episodes,
            edges=edges,
            entities=entities,
            alias_of=alias_of,
            forgotten=forgotten,
        )
