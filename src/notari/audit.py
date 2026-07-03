"""Tamper-evident audit chain over the event log.

Every appended event also appends an `AuditEntry` whose `entry_hash` chains to the
previous one: `entry_hash = H(prev_hash || payload_hash)`. Editing, inserting, or
deleting any past event breaks the chain and is detected by `verify_entries`.

The chain hashes **content digests, not raw content** — `payload_hash` for an
episode uses its `content_hash`, and a fact's object is hashed, never stored raw.
So the audit table holds no PII, and crypto-shredding a subject's content does
**not** break the chain: verification uses the stored hashes only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .events import (
    EntityMerged,
    EntityUnmerged,
    EpisodeIngested,
    Event,
    FactAsserted,
    FactInvalidated,
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


def digest(event: Event) -> tuple[str, str, str]:
    """(kind, ref, payload_hash) for an event. PII (the fact object) is hashed."""
    if isinstance(event, EpisodeIngested):
        return "episode", event.episode_id, _h(f"episode|{event.episode_id}|{event.content_hash}")
    if isinstance(event, FactAsserted):
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
                        event.valid_from.isoformat(),
                        event.source_episode_id,
                    ]
                )
            ),
        )
    if isinstance(event, FactInvalidated):
        vt = event.valid_to.isoformat() if event.valid_to else ""
        return (
            "invalidated",
            event.fact_id,
            _h(f"invalidated|{event.fact_id}|{event.reason}|{vt}"),
        )
    if isinstance(event, EntityMerged):
        return (
            "entity_merged",
            event.canonical_id,
            _h(f"entity_merged|{event.canonical_id}|{event.alias_id}"),
        )
    if isinstance(event, EntityUnmerged):
        return (
            "entity_unmerged",
            event.canonical_id,
            _h(f"entity_unmerged|{event.canonical_id}|{event.alias_id}"),
        )
    if isinstance(event, SubjectForgotten):
        return (
            "subject_forgotten",
            event.subject_id,
            _h(f"subject_forgotten|{event.subject_id}|{event.requested_by}"),
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


def verify(events: list[Event], entries: list[AuditEntry]) -> AuditReport:
    """Full verification: the chain links **and** that each stored event still
    matches the content digest committed to the chain.

    `verify_entries` alone proves the ledger wasn't reordered, truncated, or
    edited — but a tamperer could also edit an *event's content* in place and
    leave the (separate) audit chain untouched. This re-derives `digest(event)`
    and compares it to the committed `payload_hash`, so a silent content edit is
    caught at the exact seq. Detects a length mismatch (event inserted/removed
    without a matching entry) too.

    Note: this checks content that is *present*. A sanctioned crypto-shred
    destroys content by design; verify chained-only (`verify_entries`) is what
    survives erasure, and that is what `verify_audit()` uses by default.
    """
    report = verify_entries(entries)
    if not report.ok:
        return report
    if len(events) != len(entries):
        return AuditReport(
            ok=False, entries=len(entries), head=report.head,
            broken_at=min(len(events), len(entries)) + 1,
        )
    for ev, e in zip(events, entries):
        if digest(ev)[2] != e.payload_hash:  # content no longer matches its commitment
            return AuditReport(ok=False, entries=len(entries), head=report.head, broken_at=e.seq)
    return report
