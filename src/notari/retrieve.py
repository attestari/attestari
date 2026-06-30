"""Hybrid retrieval over a Projection.

Combines semantic similarity (cosine over embeddings) with keyword overlap, and
— crucially — restricts candidates to the facts valid at the requested instant.
The bi-temporal correctness comes from that candidate filter: "now" uses the live
edges; an `as_of` query uses the valid-time slice. Provenance rides along on the
returned edge so the caller can always answer *why*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .embed import Embedder, cosine
from .projection import Edge, Projection

_TOKEN = re.compile(r"[a-z0-9]+")

# Weight keyword overlap a bit higher than semantic: the default HashEmbedder is
# a crude stand-in, so lexical signal is more reliable until real embeddings land.
_SEM_WEIGHT = 0.4
_KW_WEIGHT = 0.6


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

    scored: list[SearchResult] = []
    for edge in candidates:
        sem = cosine(qvec, edge.embedding) if edge.embedding else 0.0
        etok = _tokens(edge.text())
        overlap = len(qtok & etok) / len(qtok) if qtok else 0.0
        score = _SEM_WEIGHT * sem + _KW_WEIGHT * overlap
        scored.append(SearchResult(edge=edge, score=score))

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:limit]
