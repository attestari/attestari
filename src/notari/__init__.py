"""Notari — the auditable memory layer for AI agents.

An event-sourced, bi-temporal memory engine: it runs in-memory with zero
dependencies, or durably on Postgres. See LEARN.md for how it all fits together.
"""

from __future__ import annotations

from .audit import AuditEntry, AuditReport
from .backend import InMemoryProjectionBackend, ProjectionBackend
from .crypto import EnvelopeCipher, NullCipher, cipher_from_env, generate_kek
from .embed import Embedder, HashEmbedder, SentenceTransformerEmbedder, default_embedder
from .extract import AnthropicExtractor, DeterministicExtractor, ExtractedFact, Extractor
from .memory import DeletionCertificate, Memory, Provenance
from .predicates import Cardinality, PredicateRegistry, default_registry
from .resolver import (
    EntityResolver,
    LexicalEntityResolver,
    MergeDecision,
    ResolutionResult,
    lexical_sim,
)
from .projection import Edge, Entity, Projection, Projector
from .retrieve import SearchResult, search
from .store import EventStore, InMemoryEventStore
from .store_postgres import PostgresEventStore, PostgresProjectionBackend

__version__ = "0.0.1"

__all__ = [
    "Memory",
    "DeletionCertificate",
    "Provenance",
    "Edge",
    "Entity",
    "Projection",
    "Projector",
    "SearchResult",
    "search",
    "EventStore",
    "InMemoryEventStore",
    "PostgresEventStore",
    "ProjectionBackend",
    "InMemoryProjectionBackend",
    "PostgresProjectionBackend",
    "Extractor",
    "DeterministicExtractor",
    "AnthropicExtractor",
    "ExtractedFact",
    "Embedder",
    "HashEmbedder",
    "SentenceTransformerEmbedder",
    "default_embedder",
    "EnvelopeCipher",
    "NullCipher",
    "cipher_from_env",
    "generate_kek",
    "AuditEntry",
    "AuditReport",
    "PredicateRegistry",
    "Cardinality",
    "default_registry",
    "EntityResolver",
    "LexicalEntityResolver",
    "MergeDecision",
    "ResolutionResult",
    "lexical_sim",
    "__version__",
]
