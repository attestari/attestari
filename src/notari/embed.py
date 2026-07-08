"""Embedder port + a zero-dependency hashing embedder.

The HashEmbedder is NOT good semantic search — it's a deterministic stand-in that
exercises the vector path end-to-end with no model download and no API key, so
the spike and CI prove the *plumbing*. A real embedding model swaps in
(sentence-transformers locally, or a hosted embeddings API) behind this same
protocol; retrieval quality improves, the architecture does not change.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

_TOKEN = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """`dim` + `embed`. Optionally declare `semantic = True` (read via
    `getattr`, default False) to tell retrieval the vectors carry real meaning —
    scoring then leans on the semantic channel instead of keyword overlap.
    Wrappers around an embedder should forward the attribute."""

    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Bag-of-tokens hashed into a fixed-dimensional, L2-normalised vector.

    Same tokens -> same direction, so semantically-overlapping strings land
    near each other under cosine similarity. Crude but deterministic."""

    semantic = False  # lexical stand-in: retrieval should stay keyword-led

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            # Hash each token to a bucket and a sign; accumulate.
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            bucket = h % self.dim
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vec[bucket] += sign
        return _l2_normalize(vec)


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (1.0 = identical)."""
    return sum(x * y for x, y in zip(a, b))


class SentenceTransformerEmbedder:
    """Real semantic embeddings via sentence-transformers (optional dep).

    Defaults to `all-MiniLM-L6-v2` (384-dim, L2-normalised), which matches the
    `vector(384)` columns in src/notari/db/schema.sql. Install with `notari[embeddings]`.
    Swap this in for production-quality retrieval; the architecture is unchanged.
    """

    semantic = True  # real meaning: retrieval leans on the semantic channel

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", dim: int = 384) -> None:
        from sentence_transformers import SentenceTransformer  # heavy, imported lazily

        self._model = SentenceTransformer(model_name)
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        # normalize so cosine() (a plain dot product) is correct.
        vec = self._model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]


def default_embedder() -> Embedder:
    """The best embedder available without extra setup.

    Prefers real semantic embeddings (`SentenceTransformerEmbedder`) when the
    `embeddings` extra is installed, and falls back to the zero-dependency
    `HashEmbedder` otherwise — so deployment entry points get good retrieval by
    default while the dependency-free path still works everywhere.
    """
    try:
        return SentenceTransformerEmbedder()
    except Exception:
        return HashEmbedder()
