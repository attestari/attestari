"""Tamper-evident audit chain over the event log.

Every appended event also appends an `AuditEntry` whose `entry_hash` chains to the
previous one: `entry_hash = H(prev_hash || payload_hash)`. Editing, inserting, or
deleting any past event breaks the chain and is detected by `verify_entries`.

The chain hashes **content digests, not raw content** — `payload_hash` for an
episode uses its `content_hash`, and a fact's object is hashed, never stored raw.
So the audit table holds no PII, and crypto-shredding a subject's content does
**not** break the chain: verification uses the stored hashes only.

`digest(event)` commits **every semantic field** of an event (free-text/PII
fields as sub-hashes, timestamps as epoch-microseconds so the commitment is
timezone- and storage-independent). Deep verification (`verify`) re-derives each
stored event's digest, re-checks each readable episode payload against its
`content_hash`, and aligns entries to events so a **sanctioned crypto-shred**
(key destroyed *and* `SubjectForgotten` tombstone present) is skipped while a
rogue key deletion (no tombstone) fails at the exact seq.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .events import (
    EntityMerged,
    EntityUnmerged,
    EpisodeIngested,
    Event,
    FactAsserted,
    FactInvalidated,
    Scope,
    SubjectForgotten,
)

GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditEntry:
    seq: int
    kind: str
    ref: str
    payload_hash: str
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    ok: bool
    entries: int
    head: str
    broken_at: int | None = None  # seq of the first broken link, if any


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ts(dt: datetime) -> str:
    """Commit a timestamp as epoch **microseconds** — identical however the
    datetime is stored or rendered (Postgres session timezone, isoformat
    variants), so digests survive a storage round-trip byte-for-byte."""
    return str(int(round(dt.timestamp() * 1_000_000)))


def _scope_s(scope: Scope) -> str:
    return "|".join(x or "" for x in (scope.subject_id, scope.agent_id, scope.session_id, scope.org_id))


def digest(event: Event) -> tuple[str, str, str]:
    """(kind, ref, payload_hash) for an event.

    Commits every semantic field, so an in-place edit of *any* of them —
    including provenance spans, confidence, validity windows, and scope — is
    caught by deep verification. Free-text/PII fields (`object`, `payload` via
    `content_hash`, `source_ref`, `evidence`) enter as sub-hashes, so the
    preimage itself carries no raw PII. Floats are committed at 6 decimals
    (coarser than a float4 round-trip error); timestamps as epoch-µs.
    """
    if isinstance(event, EpisodeIngested):
        return (
            "episode",
            event.episode_id,
            _h(
                "|".join(
                    [
                        "episode",
                        event.episode_id,
                        event.content_hash,  # commits the payload (sha256 of it)
                        _h(event.source_ref or ""),
                        _scope_s(event.scope),
                        _ts(event.ingested_at),
                    ]
                )
            ),
        )
    if isinstance(event, FactAsserted):
        span = f"{event.char_span[0]},{event.char_span[1]}" if event.char_span else ""
        return (
            "asserted",
            event.fact_id,
            _h(
                "|".join(
                    [
                        "asserted",
                        event.fact_id,
                        event.subject,
                        event.predicate,
                        _h(event.object),  # hash PII, never store raw
                        _ts(event.valid_from),
                        _ts(event.valid_to) if event.valid_to else "",
                        f"{event.confidence:.6f}",
                        span,
                        event.source_episode_id,
                        _scope_s(event.scope),
                        _ts(event.recorded_at),
                    ]
                )
            ),
        )
    if isinstance(event, FactInvalidated):
        return (
            "invalidated",
            event.fact_id,
            _h(
                "|".join(
                    [
                        "invalidated",
                        event.fact_id,
                        event.reason,
                        _ts(event.valid_to),
                        event.superseded_by or "",
                        _ts(event.recorded_at),
                    ]
                )
            ),
        )
    if isinstance(event, EntityMerged):
        return (
            "entity_merged",
            event.canonical_id,
            _h(
                "|".join(
                    [
                        "entity_merged",
                        event.canonical_id,
                        event.alias_id,
                        _h(event.evidence),
                        _ts(event.recorded_at),
                    ]
                )
            ),
        )
    if isinstance(event, EntityUnmerged):
        return (
            "entity_unmerged",
            event.canonical_id,
            _h(f"entity_unmerged|{event.canonical_id}|{event.alias_id}|{_ts(event.recorded_at)}"),
        )
    if isinstance(event, SubjectForgotten):
        return (
            "subject_forgotten",
            event.subject_id,
            _h(
                "|".join(
                    [
                        "subject_forgotten",
                        event.subject_id,
                        event.requested_by,
                        _ts(event.recorded_at),
                    ]
                )
            ),
        )
    raise TypeError(f"unknown event type: {type(event)!r}")  # pragma: no cover


def link(prev_hash: str, payload_hash: str) -> str:
    return _h(prev_hash + payload_hash)


def next_entry(prev_hash: str, seq: int, event: Event) -> AuditEntry:
    """The entry that commits `event` to the chain after `prev_hash`.

    Single source of truth for how the chain is extended: every store adapter
    (in-memory, Postgres) appends exactly this entry, so the guarantee cannot
    drift between deployment modes.
    """
    kind, ref, payload_hash = digest(event)
    return AuditEntry(
        seq=seq,
        kind=kind,
        ref=ref,
        payload_hash=payload_hash,
        prev_hash=prev_hash,
        entry_hash=link(prev_hash, payload_hash),
    )


def verify_entries(entries: list[AuditEntry]) -> AuditReport:
    """Walk the chain and confirm every link. Detects edit/insert/delete."""
    prev = GENESIS
    for e in entries:
        if e.prev_hash != prev or e.entry_hash != link(e.prev_hash, e.payload_hash):
            return AuditReport(ok=False, entries=len(entries), head=prev, broken_at=e.seq)
        prev = e.entry_hash
    return AuditReport(ok=True, entries=len(entries), head=prev)


def verify(
    events: list[Event],
    entries: list[AuditEntry],
    *,
    erased: frozenset[str] | set[str] = frozenset(),
) -> AuditReport:
    """Full verification: the chain links **and** that each stored event still
    matches the content digest committed to the chain.

    `verify_entries` alone proves the ledger wasn't reordered, truncated, or
    edited — but a tamperer could also edit an *event's content* in place and
    leave the (separate) audit chain untouched. This walks the entries in chain
    order and aligns them against the readable events:

    - a matching event (same kind, ref, and re-derived `payload_hash`) consumes
      the entry — and for episodes the payload is additionally re-hashed against
      `content_hash`, so an in-place payload edit that kept the stored
      `content_hash` is caught too;
    - an entry whose `ref` is in `erased` — a **sanctioned crypto-shred** (the
      store vouches: key destroyed *and* `SubjectForgotten` tombstone present) —
      is skipped: the content is gone by design, the commitment stands;
    - anything else is tampering, reported at the exact seq. In particular, a
      destroyed key **without** a tombstone does not qualify as erased, so a
      rogue key deletion fails verification instead of hiding.

    Deep verification therefore *survives* legitimate crypto-shreds; chain-only
    (`verify_entries`) remains the zero-knowledge fallback that needs no events
    at all.
    """
    report = verify_entries(entries)
    if not report.ok:
        return report

    idx = 0
    for e in entries:
        ev = events[idx] if idx < len(events) else None
        if ev is not None:
            kind, ref, payload_hash = digest(ev)
            if kind == e.kind and ref == e.ref and payload_hash == e.payload_hash:
                if isinstance(ev, EpisodeIngested) and _h(ev.payload) != ev.content_hash:
                    # Payload edited in place with the stored content_hash kept.
                    return AuditReport(
                        ok=False, entries=len(entries), head=report.head, broken_at=e.seq
                    )
                idx += 1
                continue
        if e.ref in erased:
            continue  # sanctioned erasure: event unreadable by design, entry stands
        return AuditReport(ok=False, entries=len(entries), head=report.head, broken_at=e.seq)

    if idx != len(events):
        # Events present that no chained entry vouches for (inserted unchained).
        return AuditReport(
            ok=False, entries=len(entries), head=report.head, broken_at=len(entries) + 1
        )
    return report
