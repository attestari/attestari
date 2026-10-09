"""The incremental fold gives exactly the projection the whole-log fold did.

`_reference_build` is the fold as it was up to 0.0.7, which folded the whole
log on every read: it applied every event, then dropped forgotten subjects'
lineage at the end. `Projector.apply` folds new events into an existing
projection instead, dropping a subject at their tombstone and skipping what
comes later. On random logs, folding whole or in pieces must give the
reference's projection, in the same order.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import datetime, timedelta, timezone

import pytest

from attestari import Memory
from attestari.embed import HashEmbedder
from attestari.events import (
    EntityMerged,
    EntityUnmerged,
    EpisodeIngested,
    Event,
    FactAsserted,
    FactInvalidated,
    Scope,
    SubjectForgotten,
)
from attestari.projection import Edge, Entity, Projection, Projector


def _reference_build(events: list[Event]) -> Projection:
    """The 0.0.7 fold, with edges kept as dicts because Edge is now frozen."""
    embed = HashEmbedder()
    episodes: dict[str, EpisodeIngested] = {}
    edges: dict[str, dict] = {}
    entities: dict[str, Entity] = {}
    alias_of: dict[str, str] = {}
    forgotten: set[str] = set()

    for ev in events:
        if isinstance(ev, EpisodeIngested):
            episodes[ev.episode_id] = ev
        elif isinstance(ev, FactAsserted):
            edges[ev.fact_id] = dict(
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
                embedding=embed.embed(f"{ev.subject} {ev.predicate} {ev.object}"),
            )
            entities.setdefault(ev.subject, Entity(canonical_id=ev.subject))
        elif isinstance(ev, FactInvalidated):
            edge = edges.get(ev.fact_id)
            if edge is not None:
                edge["valid_to"] = ev.valid_to
                edge["tx_to"] = ev.recorded_at
                edge["alive"] = False
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

    if forgotten:
        episodes = {eid: ep for eid, ep in episodes.items() if ep.scope.subject_id not in forgotten}
        edges = {fid: e for fid, e in edges.items() if e["subject_id"] not in forgotten}
        entities = {cid: ent for cid, ent in entities.items() if cid not in forgotten}

    return Projection(
        episodes=episodes,
        edges={fid: Edge(**e) for fid, e in edges.items()},
        entities=entities,
        alias_of=alias_of,
        forgotten=forgotten,
    )


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SCOPES = ["alice", "bob", "carol", None]
NAMES = ["alice", "bob", "Dana", "Acme", "Bobby"]  # some are also subject ids


def _random_log(rng: random.Random, length: int) -> list[Event]:
    events: list[Event] = []
    episodes: list[str] = []
    facts: list[str] = []
    for i in range(length):
        at = T0 + timedelta(minutes=i)
        kind = rng.choices(
            ["episode", "fact", "invalidate", "merge", "unmerge", "forget"],
            weights=[4, 6, 3, 1, 1, 1],
        )[0]
        if kind == "episode" or not episodes:
            eid = f"ep{i}"
            episodes.append(eid)
            events.append(
                EpisodeIngested(
                    episode_id=eid,
                    content_hash="x",
                    payload=f"message {i}",
                    scope=Scope(subject_id=rng.choice(SCOPES)),
                    ingested_at=at,
                )
            )
        elif kind == "fact":
            fid = f"f{i}"
            facts.append(fid)
            events.append(
                FactAsserted(
                    fact_id=fid,
                    subject=rng.choice(NAMES),
                    predicate=rng.choice(["lives_in", "uses"]),
                    object=rng.choice(["Berlin", "Paris", "Rust"]),
                    source_episode_id=rng.choice(episodes),
                    valid_from=at,
                    valid_to=at + timedelta(days=1) if rng.random() < 0.15 else None,
                    char_span=(0, 4) if rng.random() < 0.5 else None,
                    scope=Scope(subject_id=rng.choice(SCOPES)),
                    recorded_at=at,
                )
            )
        elif kind == "invalidate":
            fid = rng.choice(facts) if facts and rng.random() < 0.85 else f"unknown{i}"
            events.append(
                FactInvalidated(fact_id=fid, reason="superseded", valid_to=at, recorded_at=at)
            )
        elif kind == "merge":
            events.append(
                EntityMerged(
                    canonical_id=rng.choice(NAMES),
                    alias_id=rng.choice(NAMES),
                    evidence="t",
                    recorded_at=at,
                )
            )
        elif kind == "unmerge":
            events.append(
                EntityUnmerged(
                    canonical_id=rng.choice(NAMES), alias_id=rng.choice(NAMES), recorded_at=at
                )
            )
        else:  # forget; content for the subject may follow, as in logs before 0.0.8
            events.append(
                SubjectForgotten(
                    subject_id=rng.choice(["alice", "bob", "Dana"]),
                    requested_by="t",
                    recorded_at=at,
                )
            )
    return events


def _assert_same(got: Projection, want: Projection) -> None:
    assert list(got.episodes.items()) == list(want.episodes.items())
    assert list(got.edges.items()) == list(want.edges.items())
    assert list(got.entities.items()) == list(want.entities.items())
    assert got.alias_of == want.alias_of
    assert got.forgotten == want.forgotten


def _frozen_view(p: Projection) -> tuple:
    return (
        list(p.episodes.items()),
        list(p.edges.items()),
        [(cid, set(ent.aliases)) for cid, ent in p.entities.items()],
        dict(p.alias_of),
        set(p.forgotten),
    )


@pytest.mark.parametrize("seed", range(300))
def test_folding_whole_or_in_pieces_matches_the_reference(seed: int) -> None:
    rng = random.Random(seed)
    events = _random_log(rng, rng.randrange(0, 45))
    want = _reference_build(events)
    projector = Projector(HashEmbedder())

    _assert_same(projector.build(events), want)

    cuts = range(len(events) + 1) if len(events) <= 12 else rng.sample(range(len(events) + 1), 6)
    for cut in cuts:
        head = projector.build(events[:cut])
        before = _frozen_view(head)
        _assert_same(projector.apply(head, events[cut:]), want)
        assert _frozen_view(head) == before  # folding on top left `head` as it was

    # One event at a time, as a reader that checks after every write.
    p = projector.empty()
    for ev in events:
        p = projector.apply(p, [ev])
    _assert_same(p, want)


def test_results_already_returned_never_change() -> None:
    mem = Memory()
    mem.add("I live in Berlin.", subject_id="u1")
    held = mem.timeline(subject_id="u1")
    mem.add("I moved to Paris.", subject_id="u1")

    assert [(e.object, e.alive) for e in held] == [("Berlin", True)]
    assert [(e.object, e.alive) for e in mem.timeline(subject_id="u1")] == [
        ("Berlin", False),
        ("Paris", True),
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        held[0].alive = False


def test_a_forget_clears_the_subjects_fact_texts_from_the_embedding_memo() -> None:
    def fact(fid: str, scope: str, subject: str, obj: str) -> FactAsserted:
        return FactAsserted(
            fact_id=fid,
            subject=subject,
            predicate="lives_in",
            object=obj,
            source_episode_id="ep",
            valid_from=T0,
            scope=Scope(subject_id=scope),
        )

    projector = Projector(HashEmbedder())
    p = projector.build(
        [
            fact("f1", "alice", "Dana", "Berlin"),  # the same text from two subjects
            fact("f2", "bob", "Dana", "Berlin"),
            fact("f3", "alice", "alice", "Oslo"),
        ]
    )
    memo = projector._embed_cache
    assert {"Dana lives_in Berlin", "alice lives_in Oslo"} <= set(memo)

    p = projector.apply(p, [SubjectForgotten(subject_id="alice", requested_by="t")])
    assert "alice lives_in Oslo" not in memo
    assert "Dana lives_in Berlin" in memo  # bob's fact still uses it

    projector.apply(p, [SubjectForgotten(subject_id="bob", requested_by="t")])
    assert "Dana lives_in Berlin" not in memo
