"""default_embedder() selection + zero-dependency fallback."""

from __future__ import annotations

import pytest

import attestari.embed as embed
from attestari import HashEmbedder


def test_default_embedder_falls_back_to_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the `embeddings` extra not being installed.
    class _Boom:
        def __init__(self, *a, **k):
            raise ImportError("sentence-transformers not installed")

    monkeypatch.setattr(embed, "SentenceTransformerEmbedder", _Boom)
    emb = embed.default_embedder()
    assert isinstance(emb, HashEmbedder)
    assert emb.dim == 384


def test_default_embedder_prefers_real_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Fake:  # stand-in for a successfully-constructed real embedder
        def __init__(self, *a, **k):
            self.dim = 384

    monkeypatch.setattr(embed, "SentenceTransformerEmbedder", _Fake)
    assert isinstance(embed.default_embedder(), _Fake)
