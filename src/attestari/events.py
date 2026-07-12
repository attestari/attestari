"""Event types — the append-only source of truth.

Everything Attestari can answer (the graph, the vector index, time-travel queries,
provable deletion) is a fold over a stream of these events. They are immutable
dataclasses; the store appends them and the projection folds them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Timezone-aware current instant (system/transaction time)."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Scope:
    """Who/what a piece of memory belongs to. `subject_id` is the data subject
    (e.g. the end user) and is the unit of deletion for GDPR purposes."""

    subject_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    org_id: str | None = None


@dataclass(frozen=True, slots=True)
class EpisodeIngested:
    """Raw ingested material. Every derived fact traces back to one episode.
    `content_hash` makes provenance tamper-evident; `payload` is the raw text
    (in production this is encrypted with a per-subject key)."""

    episode_id: str
    content_hash: str
    payload: str
    scope: Scope
    ingested_at: datetime = field(default_factory=utcnow)
    source_ref: str | None = None
    op: str = "episode_ingested"


@dataclass(frozen=True, slots=True)
class FactAsserted:
    """A (subject, predicate, object) triple believed true from `valid_from`.

    `valid_*` is VALID time (true-in-world); `recorded_at` is SYSTEM time (when
    Attestari learned it). `source_episode_id` + `char_span` are the provenance."""

    fact_id: str
    subject: str
    predicate: str
    object: str
    source_episode_id: str
    valid_from: datetime
    valid_to: datetime | None = None
    confidence: float = 1.0
    char_span: tuple[int, int] | None = None
    scope: Scope = field(default_factory=Scope)
    recorded_at: datetime = field(default_factory=utcnow)
    op: str = "fact_asserted"


@dataclass(frozen=True, slots=True)
class FactInvalidated:
    """Closes a previously asserted fact's valid-time window (a correction).
    The old fact is retained for audit; it just stops being `alive`."""

    fact_id: str
    reason: str
    valid_to: datetime
    superseded_by: str | None = None
    recorded_at: datetime = field(default_factory=utcnow)
    op: str = "fact_invalidated"


@dataclass(frozen=True, slots=True)
class EntityMerged:
    """Records that `alias_id` is the same real-world entity as `canonical_id`
    (the "same person, three names" resolution). Reversible by design."""

    canonical_id: str
    alias_id: str
    evidence: str
    recorded_at: datetime = field(default_factory=utcnow)
    op: str = "entity_merged"


@dataclass(frozen=True, slots=True)
class EntityUnmerged:
    """Reverses an EntityMerged — splits an alias back out of its canonical
    entity. Entity resolution is reversible by design."""

    canonical_id: str
    alias_id: str
    recorded_at: datetime = field(default_factory=utcnow)
    op: str = "entity_unmerged"


@dataclass(frozen=True, slots=True)
class SubjectForgotten:
    """Right-to-be-forgotten. Triggers destruction of the subject's lineage on
    the next projection rebuild, and issuance of a deletion certificate."""

    subject_id: str
    requested_by: str
    recorded_at: datetime = field(default_factory=utcnow)
    op: str = "subject_forgotten"


# The closed set of events the projection knows how to fold.
Event = (
    EpisodeIngested
    | FactAsserted
    | FactInvalidated
    | EntityMerged
    | EntityUnmerged
    | SubjectForgotten
)
