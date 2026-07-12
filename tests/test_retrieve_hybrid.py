"""Hybrid-retrieval scoring: embedder-aware weights, determinism, parity.

The scheme under test (retrieve.py, mirrored by the Postgres adapter's SQL):
weighted linear blend of the semantic and keyword channels, with weights chosen
by the embedder's `semantic` honesty flag and deterministic tie-breaks
(earliest-established tx_from, then fact_id) shared by both backends.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from attestari import HashEmbedder, Memory
from attestari.events import FactAsserted
from attestari.projection import Projector
from attestari.retrieve import search, weights_for


class _VecEmbedder:
    """Hand-crafted vectors: anything mentioning Rust shares the query's
    direction (semantic winner); everything else is orthogonal."""

    dim = 4
    semantic = False  # flipped per-test

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0] if "rust" in text.lower() else [0.0, 1.0, 0.0, 0.0]


def _fact(fid: str, subject: str, predicate: str, obj: str, year: int) -> FactAsserted:
    return FactAsserted(
        fact_id=fid,
        subject=subject,
        predicate=predicate,
        object=obj,
        source_episode_id="e1",
        valid_from=datetime(year, 1, 1, tzinfo=timezone.utc),
    )


def test_weights_follow_embedder_honesty() -> None:
    assert weights_for(HashEmbedder()) == (0.4, 0.6)  # lexical -> keyword-led

    class Real:
        dim = 4
        semantic = True

        def embed(self, text: str) -> list[float]:
            return [0.0] * 4

    assert weights_for(Real()) == (0.7, 0.3)  # real model -> semantic-led

    class Undeclared:
        dim = 4

        def embed(self, text: str) -> list[float]:
            return [0.0] * 4

    # Fail-safe: unknown vector quality is treated as lexical.
    assert weights_for(Undeclared()) == (0.4, 0.6)


def test_semantic_flag_flips_the_ranking() -> None:
    """Each edge wins one channel; the embedder's honesty decides which leads.

    Query "alice speaks language french": the French fact wins the keyword
    channel (overlap 1.0 vs 0.25); the Rust fact wins the semantic channel
    (its vector matches the query's). Same data, same query — only the
    `semantic` flag differs, and the top result flips.
    """
    q = "alice speaks language french"

    class QueryAligned(_VecEmbedder):
        # The query shares the Rust fact's direction (semantic winner) while
        # its tokens overlap the French fact harder (keyword winner).
        def embed(self, text: str) -> list[float]:
            if "rust" in text.lower() or text == q:
                return [1.0, 0.0, 0.0, 0.0]
            return [0.0, 1.0, 0.0, 0.0]

    proj = Projector(QueryAligned()).build(
        [
            _fact("fa", "alice", "uses", "Rust", 2020),
            _fact("fb", "alice", "speaks_language", "French", 2020),
        ]
    )

    lexical = QueryAligned()
    lexical.semantic = False
    top_lexical = search(proj, q, lexical, limit=2)[0].edge.fact_id

    semantic = QueryAligned()
    semantic.semantic = True
    top_semantic = search(proj, q, semantic, limit=2)[0].edge.fact_id

    assert top_lexical == "fb"  # keyword-led: the French fact's overlap wins
    assert top_semantic == "fa"  # semantic-led: the Rust fact's vector wins


def test_deterministic_tiebreak_established_then_fact_id() -> None:
    """Exact score ties order by earliest-established (tx_from asc), then
    fact_id — stable across runs and (by construction) across backends, and
    established memory doesn't reshuffle when equal-scoring facts arrive."""
    emb = HashEmbedder()
    projector = Projector(emb)
    proj = projector.build(
        [
            _fact("f-old", "alice", "visited", "Lisbon", 2019),
            _fact("f-new", "alice", "visited", "Lisbon", 2023),
            _fact("f-a", "alice", "visited", "Porto", 2021),
            _fact("f-b", "bob", "visited", "Porto", 2021),
        ]
    )
    results = search(proj, "who visited lisbon", emb, limit=4)
    lisbon = [r.edge.fact_id for r in results if r.edge.object == "Lisbon"]
    assert lisbon == ["f-old", "f-new"]  # earliest-established wins the tie

    results = search(proj, "porto", emb, limit=4)
    porto = [r.edge.fact_id for r in results if r.edge.object == "Porto"]
    assert porto == ["f-a", "f-b"]  # then fact_id asc


DSN = os.environ.get("ATTESTARI_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="set ATTESTARI_DATABASE_URL to run Postgres tests")
def test_backend_ranking_parity() -> None:
    """The same clear-cut queries rank the same facts first on both backends.

    (The keyword scorers differ — token overlap vs ts_rank — so parity is
    asserted on unambiguous fixtures, not arbitrary corpora.)
    """
    from attestari import PostgresEventStore

    store = PostgresEventStore(DSN)
    store.truncate()
    store.close()

    corpus = [
        ("Hi, I'm Alice. I live in Berlin.", "u1"),
        ("I work at Acme.", "u1"),
        ("Hi, I'm Bob. I live in Paris.", "u2"),
        ("I use Rust.", "u2"),
    ]
    mem_local = Memory(embedder=HashEmbedder())
    mem_pg = Memory.postgres(DSN, embedder=HashEmbedder())
    for text, sid in corpus:
        mem_local.add(text, subject_id=sid)
        mem_pg.add(text, subject_id=sid)

    queries = ["where does alice live", "who works at acme", "who uses rust"]
    for q in queries:
        top_local = mem_local.search(q, limit=1)[0].edge
        top_pg = mem_pg.search(q, limit=1)[0].edge
        assert (top_local.subject, top_local.predicate, top_local.object) == (
            top_pg.subject,
            top_pg.predicate,
            top_pg.object,
        ), f"backends disagree on {q!r}"

    store = PostgresEventStore(DSN)
    store.truncate()
    store.close()
