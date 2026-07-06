"""Hybrid retrieval over a Projection.

Fuses two channels — semantic similarity (cosine over embeddings) and keyword
overlap — with a weighted linear blend, and — crucially — restricts candidates
to the facts valid at the requested instant. The bi-temporal correctness comes
from that candidate filter: "now" uses the live edges; an `as_of` query uses
the valid-time slice. Provenance rides along on the returned edge so the caller
can always answer *why*.

The channel *weights* follow the embedder's honesty: a lexical stand-in
(HashEmbedder) keeps retrieval keyword-led, because its "semantic" channel is
really just noisy token overlap; a real model (`semantic = True`) flips it
semantic-led. Ordering is fully deterministic — exact score ties are broken by
earliest-established (`tx_from`), then `fact_id` — and the Postgres adapter
uses the same weights and tie-breaks, so a query ranks identically regardless
of deployment mode.

(Rank fusion — weighted RRF — was evaluated as an alternative and *rejected on
measurement*: flattening scores into ranks discards the keyword-overlap
magnitude, which is discriminative here; see internal PROOF §5.4 for the
sweep. The blend keeps magnitudes and won on LOCOMO recall@10.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .embed import Embedder, cosine
from .projection import Edge, Projection

_TOKEN = re.compile(r"[a-z0-9]+")

# (sem_weight, kw_weight) per embedder honesty. Both store backends import
# these via weights_for — single source of truth.
_LEXICAL_WEIGHTS = (0.4, 0.6)  # hash embedder: lexical signal is more reliable
_SEMANTIC_WEIGHTS = (0.7, 0.3)  # real embeddings: meaning leads


def weights_for(embedder: Embedder) -> tuple[float, float]:
    """(sem_weight, kw_weight) for this embedder. Fail-safe: an embedder that
    doesn't declare `semantic = True` is treated as lexical — keyword-led is the
    honest default when vector quality is unknown."""
    return _SEMANTIC_WEIGHTS if getattr(embedder, "semantic", False) else _LEXICAL_WEIGHTS


@dataclass
class SearchResult:
    edge: Edge
    score: float


def _norm(tok: str) -> str:
    # Cheap stemmer: fold trivial plural/3rd-person forms (lives -> live).
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") else tok


def _tokens(text: str) -> set[str]:
    return {_norm(t) for t in _TOKEN.findall(text.lower())}


def search(
    projection: Projection,
    query: str,
    embedder: Embedder,
    *,
    subject_id: str | None = None,
    as_of: datetime | None = None,
    limit: int = 5,
) -> list[SearchResult]:
    candidates = (
        projection.edges_asof(as_of, subject_id)
        if as_of is not None
        else projection.live_edges(subject_id)
    )
    if not candidates:
        return []

    qvec = embedder.embed(query)
    qtok = _tokens(query)
    w_sem, w_kw = weights_for(embedder)

    scored: list[SearchResult] = []
    for edge in candidates:
        sem = cosine(qvec, edge.embedding) if edge.embedding else 0.0
        overlap = len(qtok & _tokens(edge.text())) / len(qtok) if qtok else 0.0
        scored.append(SearchResult(edge=edge, score=w_sem * sem + w_kw * overlap))

    # Deterministic ordering, shared with the Postgres adapter: score desc,
    # then earliest-established (tx_from) wins exact ties — established memory
    # doesn't reshuffle when equal-scoring facts arrive later — then fact_id.
    scored.sort(key=lambda r: (-r.score, r.edge.tx_from.timestamp(), r.edge.fact_id))
    return scored[:limit]
