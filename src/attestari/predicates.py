"""Predicate cardinality registry.

Treating every predicate as single-valued is wrong: "I use Python" then "I use
Rust" would *supersede* — only Rust survives. Real memory needs both:

- **single-valued** (`lives_in`, `works_at`, `name_is`): a new value supersedes
  the old (latest-wins) — the previous fact is invalidated but kept for audit.
- **multi-valued** (`uses`, `speaks_language`, `likes`): values **coexist**; a new
  one is simply added (with exact-duplicate dedup).

Unknown predicates default to single-valued. The registry is configurable.
"""

from __future__ import annotations

from enum import Enum


class Cardinality(str, Enum):
    SINGLE = "single"
    MULTI = "multi"


# Predicates a subject naturally holds many of at once.
_DEFAULT_MULTI = {
    "uses",
    "speaks_language",
    "likes",
    "knows",
    "owns",
    "visited",
    "interested_in",
    "member_of",
}


class PredicateRegistry:
    def __init__(
        self,
        multi: set[str] | None = None,
        default: Cardinality = Cardinality.SINGLE,
    ) -> None:
        self._multi = set(_DEFAULT_MULTI if multi is None else multi)
        self._default = default

    def cardinality(self, predicate: str) -> Cardinality:
        return Cardinality.MULTI if predicate in self._multi else self._default

    def is_single_valued(self, predicate: str) -> bool:
        return self.cardinality(predicate) == Cardinality.SINGLE

    def declare(self, predicate: str, cardinality: Cardinality) -> None:
        if cardinality == Cardinality.MULTI:
            self._multi.add(predicate)
        else:
            self._multi.discard(predicate)


def default_registry() -> PredicateRegistry:
    return PredicateRegistry()
