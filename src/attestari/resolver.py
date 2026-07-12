"""Entity resolution: the "same entity, many surface forms" problem.

A candidate -> score -> decide pipeline behind a port. The lexical resolver needs
no model: it scores name pairs by token containment / Jaccard / edit similarity,
blended with embedding cosine, and bands the decision:

    score >= tau_high   -> auto-merge
    tau_low..tau_high   -> surface for review (no silent bad merges)
    score <  tau_low    -> ignore

Merges are emitted as reversible `EntityMerged` events (undo via `EntityUnmerged`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .embed import Embedder, HashEmbedder, cosine

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(name: str) -> set[str]:
    return set(_WORD.findall(name.lower()))


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _edit_sim(a: str, b: str) -> float:
    a, b = a.lower(), b.lower()
    m = max(len(a), len(b))
    return 1.0 if m == 0 else 1.0 - _levenshtein(a, b) / m


def lexical_sim(a: str, b: str) -> float:
    """0..1 name similarity. One name being a qualified form of the other
    (token-subset, e.g. 'Acme' vs 'Acme Corp') scores high."""
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.9
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(jaccard, _edit_sim(a, b))


@dataclass(frozen=True, slots=True)
class MergeDecision:
    canonical: str
    alias: str
    score: float


@dataclass
class ResolutionResult:
    auto_merges: list[MergeDecision] = field(default_factory=list)
    review: list[MergeDecision] = field(default_factory=list)


@runtime_checkable
class EntityResolver(Protocol):
    def resolve(self, names: list[str]) -> ResolutionResult: ...


class LexicalEntityResolver:
    def __init__(
        self,
        embedder: Embedder | None = None,
        tau_high: float = 0.85,
        tau_low: float = 0.5,
    ) -> None:
        self.embedder = embedder or HashEmbedder()
        self.tau_high = tau_high
        self.tau_low = tau_low

    def _score(self, a: str, b: str) -> float:
        lex = lexical_sim(a, b)
        sem = cosine(self.embedder.embed(a), self.embedder.embed(b))
        return max(lex, sem)

    def resolve(self, names: list[str]) -> ResolutionResult:
        uniq = sorted(set(names))
        result = ResolutionResult()
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                score = self._score(a, b)
                if score < self.tau_low:
                    continue
                # Canonical = the longer (more complete) surface form.
                canonical, alias = (a, b) if len(a) >= len(b) else (b, a)
                decision = MergeDecision(canonical=canonical, alias=alias, score=round(score, 3))
                bucket = result.auto_merges if score >= self.tau_high else result.review
                bucket.append(decision)
        return result
