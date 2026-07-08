"""Notari — the auditable memory layer for AI agents.

An event-sourced, bi-temporal memory engine. Three storage tiers, one engine:
in-memory with zero dependencies (`Memory()`), durable in a local SQLite file
with zero infrastructure (`Memory.local()`), or a concurrent production service
on Postgres + pgvector (`Memory.postgres()`). See LEARN.md for how it fits
together.
"""

from __future__ import annotations

from .audit import AuditEntry, AuditReport, verify, verify_entries
from .backend import InMemoryProjectionBackend, ProjectionBackend
from .crypto import EnvelopeCipher, NullCipher, cipher_from_env, generate_kek
from .embed import Embedder, HashEmbedder, SentenceTransformerEmbedder, default_embedder
from .extract import (
    AnthropicExtractor,
    DeterministicExtractor,
    ExtractedFact,
    Extractor,
    default_extractor,
)
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
from .store_sqlite import SQLiteEventStore

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
    "SQLiteEventStore",
    "PostgresEventStore",
    "ProjectionBackend",
    "InMemoryProjectionBackend",
    "PostgresProjectionBackend",
    "Extractor",
    "DeterministicExtractor",
    "AnthropicExtractor",
    "default_extractor",
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
    "verify",
    "verify_entries",
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
