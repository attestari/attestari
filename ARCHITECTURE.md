# Architecture

Notari is an **event-sourced, bi-temporal memory engine** with a **hexagonal**
(ports-and-adapters) shape. It runs in-memory with zero dependencies, or durably
on Postgres + pgvector — the same engine, the same guarantees, behind swappable
adapters.

## The one idea everything follows from

> The append-only **event log is the source of truth.** Everything you query —
> the knowledge graph, the vector index, the keyword index — is a *projection*
> you can throw away and rebuild from the log.

That single decision is why the hard features fall out of the design instead of
being bolted on:

- **Audit & provenance** — the log already records who said what, when, and from
  which source episode. There's nothing extra to maintain.
- **Time travel** — every fact carries valid-time *and* system-time, so "what did
  the agent believe on date D?" is just a filtered fold of the log.
- **Provable deletion** — "forget subject X" is itself an event; rebuilding the
  projections without X's lineage genuinely removes it, and a signed certificate
  is emitted as proof. The raw payload is encrypted per-subject and the key is
  destroyed ("crypto-shredding"), so replicas and backups are covered too.

## Components

```
            add(message, scope)
                   │
                   ▼
   ┌───────────────────────────────┐
   │ extract.py  Extractor (port)   │   DeterministicExtractor (zero-dep)
   │  text ─▶ [ (subj, pred, obj) ] │   AnthropicExtractor (Claude structured output)
   └───────────────┬───────────────┘
                   ▼  append-only
   ┌───────────────────────────────┐
   │ store.py    EventStore (port)  │   InMemoryEventStore | PostgresEventStore
   │   EpisodeIngested              │
   │   FactAsserted / FactInvalidated
   │   EntityMerged / EntityUnmerged │
   │   SubjectForgotten             │
   └───────────────┬───────────────┘
                   ▼  fold / project (rebuildable)
   ┌───────────────────────────────┐
   │ projection.py  Projection      │   entities + edges (bi-temporal) + vectors
   │   embed.py  Embedder (port)    │   HashEmbedder | SentenceTransformerEmbedder
   └───────────────┬───────────────┘
                   ▼
   ┌───────────────────────────────┐
   │ retrieve.py  hybrid retrieval  │   semantic ⊕ keyword ⊕ graph, + "as-of" filter
   └───────────────┬───────────────┘
                   ▼
   memory.py  Memory facade:
     add / search / answer / timeline / get_provenance / conflicts /
     forget / verify_audit / resolve_entities
```

### Ports are swappable (hexagonal)

`Extractor`, `EventStore`, `Embedder`, and `ProjectionBackend` are Python
`Protocol`s. The batteries-included defaults are dependency-free, so the engine
runs anywhere; Postgres, real embeddings, and Claude swap in behind the same
protocols without touching the core logic. The cleanest contributions add a new
adapter and change nothing else.

## The event log

Every write is an immutable, totally-ordered event ([events.py](src/notari/events.py)):

| Event | Meaning |
|---|---|
| `EpisodeIngested` | A raw message arrived (content hash, payload, scope, source ref). |
| `FactAsserted` | A `(subject, predicate, object)` triple was learned, with valid-time, confidence, and a link back to its source episode. |
| `FactInvalidated` | A prior fact stopped being true (superseded or corrected); the row is **retained**, its `valid_to` is closed. |
| `EntityMerged` / `EntityUnmerged` | Two surface forms are (or are no longer) the same entity — reversible. |
| `SubjectForgotten` | A right-to-be-forgotten request for one subject's entire scope. |

A `Scope` (`subject_id`, `agent_id`, `session_id`, `org_id`) rides on writes so
facts can be partitioned and queried per user, agent, session, or org.

## Bi-temporal model

Each fact lives on two independent time axes:

| Axis | Field | Question it answers |
|---|---|---|
| **Valid time** | `valid_from` / `valid_to` | When was this true *in the world*? |
| **System time** | `tx_from` / `tx_to` | When did *Notari* know it? |

A correction ("actually they moved in March") is a **new** assertion plus an
invalidation of the old one. The old fact is never edited or deleted — only its
`valid_to` is closed — so the full history is reconstructable. Querying `as_of` a
past instant restricts to the facts whose valid-time interval contains it; the
default is "now."

## Retrieval

Hybrid, with time built in ([retrieve.py](src/notari/retrieve.py)):

1. **semantic** — cosine similarity over fact embeddings (pgvector HNSW on Postgres);
2. **keyword** — lexical token overlap (Postgres full-text ranking on the durable path);
3. **graph** — neighbours of matched entities;
4. **temporal filter** — restrict to facts valid at the requested `as_of` instant.

The channel weights follow the embedder: with the zero-dependency hash embedder
retrieval stays keyword-led; a real embedding model (declared via the port's
`semantic` flag) flips it semantic-led. Ordering is deterministic (exact ties
prefer the earliest-established fact) and identical across the in-memory and
Postgres backends.

Every result carries its provenance, so a caller can always answer *why* a memory
was returned. `Memory.answer()` is a thin convenience over `search()` that returns
the single top object.

## Storage backends

The write side is an **`EventStore`** port with three adapters — in-memory
([store.py](src/notari/store.py)), a single local SQLite file
([store_sqlite.py](src/notari/store_sqlite.py), stdlib only: durable with zero
infrastructure, for single-process agents and MCP), and Postgres
([store_postgres.py](src/notari/store_postgres.py)). All three share the same
audit-chain and key-lifecycle components, so the guarantees cannot drift
between tiers.

The read/query side sits behind a **`ProjectionBackend`** port
([backend.py](src/notari/backend.py)) with two implementations:

- **`InMemoryProjectionBackend`** — folds the event log on every read (used by
  the in-memory and SQLite tiers). Zero dependencies and fully deterministic;
  this is the reference behaviour the other backend is checked against.
- **`PostgresProjectionBackend`** ([store_postgres.py](src/notari/store_postgres.py))
  — a durable **`PostgresEventStore`** plus **materialised** `entity`/`edge`
  projection tables (rebuildable from the log), with **hybrid retrieval evaluated
  in SQL**: pgvector cosine + Postgres full-text + the bi-temporal `as_of` filter.
  This lights up the HNSW index defined in [src/notari/db/schema.sql](src/notari/db/schema.sql).

`Memory.postgres()` wires the durable store and backend together. The whole thing
runs on **one Postgres + pgvector container — no graph database required.**

## Deletion / right-to-be-forgotten

`forget(subject_id)` ([memory.py](src/notari/memory.py)):

1. appends a `SubjectForgotten` event;
2. rebuilds the projections, dropping every episode and fact in that subject's
   scope (their entire lineage);
3. returns a **`DeletionCertificate`** — subject, counts, a manifest hash, the
   operator, and the timestamp.

You keep the certificate as proof it happened; the content is gone.

**Crypto-shred** ([crypto.py](src/notari/crypto.py)) makes that erasure provable
even against backups: each subject's PII is encrypted at rest with a per-subject
data key (wrapped under a root KEK). `forget()` destroys the data key, so the
ciphertext is permanently unrecoverable while the immutable rows and the signed
certificate remain. This resolves the usual tension between event sourcing ("never
delete") and erasure ("delete on request"). Opt in via the `NOTARI_KEK`
environment variable.

## Tamper-evident audit chain

Every event is hash-linked ([audit.py](src/notari/audit.py)):
`entry_hash = H(prev_hash || payload_hash)`. `verify_audit()` walks the chain and
detects any edit, insertion, or deletion. Because the chain hashes *content
digests* — not raw payloads — **the proof survives crypto-shred**: you can still
verify the history wasn't altered after a subject's content has been destroyed.

## Conflict resolution & entity resolution

- **Predicate cardinality** ([predicates.py](src/notari/predicates.py)) —
  predicates are single-valued (`lives_in`, `works_at` — a new value supersedes
  the old) or multi-valued (`uses_tool` — values coexist). `conflicts()` surfaces
  resolved conflicts rather than hiding them.
- **Entity resolution** ([resolver.py](src/notari/resolver.py)) — a
  candidate → score → merge pipeline with auto-merge and human-review bands;
  every merge is reversible via `EntityUnmerged`.

## Surfaces

The same engine is exposed four ways:

- **Python facade** — `from notari import Memory` ([memory.py](src/notari/memory.py)).
- **REST API + console** — a FastAPI app ([server.py](src/notari/server.py)) at
  `/v1/*`, with a zero-build graph console ([console.py](src/notari/console.py))
  served at `/`.
- **MCP server** ([mcp.py](src/notari/mcp.py)) — `add_memory`, `search_memory`,
  `get_provenance`, `forget_subject` for any agent that speaks MCP.
- **TypeScript SDK** ([clients/ts](clients/ts)) — a thin typed client mirroring
  the `Memory` surface over the REST API.

## Scaling — an infinite log at finite cost

Event sourcing never erases, which raises the obvious question: does storage blow
up at scale? In practice, no — this is a well-trodden pattern (ledgers, Kafka,
EventStoreDB), and the events themselves are tiny.

**Sizing intuition.** A fact event is ~300 bytes, so **1B facts ≈ 300 GB** — tens
of dollars a month on SSD, a few dollars on object storage. The append-only log is
not the scaling liability; raw payloads are. A 50-page PDF dwarfs the handful of
facts extracted from it.

The append-only log plus a disposable, rebuildable projection make the standard
toolkit clean to introduce — each is additive and slots in behind the existing
ports without rearchitecting:

- **Blob externalisation** — keep raw payloads in content-addressed object storage
  (S3), and only the `content_hash` + small fact events in the hot DB. The schema
  already records `content_hash`.
- **Snapshots** — persist the projection "as of event N" and replay only events
  after it, so reads stay O(recent) rather than O(history).
- **Incremental projection** — update the materialised `entity`/`edge` tables per
  event instead of rebuilding, behind the same `ProjectionBackend` port.
- **Partitioning / tiering** — split the log by subject/org/time; keep recent
  events on SSD and archive old ones to cheap storage.
- **Crypto-shred** already shrinks forgotten subjects to metadata only.

New to these ideas? [LEARN.md](LEARN.md) explains the whole design from scratch.
