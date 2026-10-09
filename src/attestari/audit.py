"""Tamper-evident audit chain over the event log.

Every appended event also appends an `AuditEntry` whose `entry_hash` chains to the
previous one: `entry_hash = H(prev_hash || payload_hash)`. Editing, inserting, or
deleting any past event breaks the chain and is detected by `verify_entries`.

The chain commits to content through **commitments, never raw content**: an
episode through its `content_hash`, a fact's object through its `object_hash`
or a sha256 of the object. When the content is encrypted (a KEK is set and the
event has a subject), both commitments are keyed: HMAC-SHA256 under a key
derived from the subject's DEK (`crypto.KeyManager.seal`). Shredding the DEK
destroys that key, so a retained commitment can't be tested against a guessed
value. An unkeyed sha256 commitment (no KEK, or an entry written before keyed
commitments) can be: anyone holding the log can confirm a guess. Either way,
crypto-shredding does **not** break the chain, because verification uses the
stored commitments only. An event's other fields (subject, predicate, scope
ids, timestamps, spans) are committed as they are and stay readable in the log.

`digest(event)` commits **every semantic field** of an event (free-text fields
as commitments, timestamps as epoch-microseconds so the commitment is
timezone- and storage-independent). Deep verification (`verify`) re-derives each
stored event's digest, re-checks each readable episode payload and sealed fact
object against its commitment, and aligns entries to events so a **sanctioned
crypto-shred** (key destroyed *and* `SubjectForgotten` tombstone present) is
skipped while a rogue key deletion (no tombstone) fails at the exact seq.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .crypto import COMMIT_PREFIX, keyed_commitment
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
    caught by deep verification. Free-text fields enter as commitments, never
    raw: `payload` via `content_hash`, `object` via `object_hash` when sealed
    (else its sha256), and `source_ref` and `evidence` as sha256. Floats are
    committed at 6 decimals (coarser than a float4 round-trip error);
    timestamps as epoch-µs.
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
                        event.content_hash,  # commits the payload (sha256, or keyed)
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
                        event.object_hash or _h(event.object),  # never the raw object
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


def _commitment_holds(
    commitment: str, text: str, subject_id: str | None, keys: Mapping[str, bytes]
) -> bool:
    if commitment.startswith(COMMIT_PREFIX):
        key = keys.get(subject_id) if subject_id else None
        return key is not None and hmac.compare_digest(keyed_commitment(key, text), commitment)
    return _h(text) == commitment


def _content_matches(ev: Event, keys: Mapping[str, bytes]) -> bool:
    """Does a readable event's content still match its stored commitment?"""
    if isinstance(ev, EpisodeIngested):
        return _commitment_holds(ev.content_hash, ev.payload, ev.scope.subject_id, keys)
    if isinstance(ev, FactAsserted) and ev.object_hash is not None:
        return _commitment_holds(ev.object_hash, ev.object, ev.scope.subject_id, keys)
    return True  # an unsealed fact's object is hashed inside its digest


def verify(
    events: list[Event],
    entries: list[AuditEntry],
    *,
    erased: frozenset[str] | set[str] = frozenset(),
    commit_keys: Mapping[str, bytes] | None = None,
) -> AuditReport:
    """Full verification: the chain links **and** that each stored event still
    matches the content digest committed to the chain.

    `verify_entries` alone proves the ledger wasn't reordered, truncated, or
    edited — but a tamperer could also edit an *event's content* in place and
    leave the (separate) audit chain untouched. This walks the entries in chain
    order and aligns them against the readable events:

    - a matching event (same kind, ref, and re-derived `payload_hash`) consumes
      the entry, and its content is checked against its stored commitment (an
      episode's payload against `content_hash`, a sealed fact's object against
      `object_hash`), so an in-place content edit that kept the commitment is
      caught too. Keyed commitments are re-derived with the subject's key from
      `commit_keys` (subject_id -> commitment key); one whose key is missing
      fails;
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

    keys = commit_keys or {}
    idx = 0
    for e in entries:
        ev = events[idx] if idx < len(events) else None
        if ev is not None:
            kind, ref, payload_hash = digest(ev)
            if kind == e.kind and ref == e.ref and payload_hash == e.payload_hash:
                if not _content_matches(ev, keys):
                    # Content edited in place with its stored commitment kept.
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
