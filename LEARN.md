# Learn Notari — how it's built (a beginner's guide)

This guide explains **what you built and why**, assuming you know basic
programming (variables, functions, maybe classes) but not the bigger patterns.
Read Part 1 first — once those 6 ideas click, every file makes sense.

---

## Part 1 — The 6 big ideas

### Idea 1: The event log (a diary you never erase)

Most apps store **the current state**: a row in a table that you UPDATE and
overwrite. Notari does the opposite. It stores a **list of things that happened**,
in order, and never edits past entries. That list is the *event log*.

Think of a bank: it doesn't store just "balance = $100." It stores every
deposit and withdrawal. Your balance is *computed* from that history. Notari
stores events like "this fact was learned," "this fact was corrected," "this
subject was forgotten."

Why this is powerful: if you keep the full history, you can answer questions like
"what did we know last March?" and "where did this come from?" for free — the
information is already there. This is called **event sourcing**.

### Idea 2: Projections (a summary you can always rebuild)

A long diary is hard to query directly ("who does the user work for *right now*?").
So we *fold* the event log into a convenient summary — a graph of facts. That
summary is a **projection**.

The key rule: **the projection is disposable.** You can delete it and rebuild it
from the event log at any time. The log is the truth; the projection is just a
fast lookup you regenerate. (The fancy name for "writes go to the log, reads come
from a rebuilt summary" is **CQRS** — Command Query Responsibility Segregation.)

### Idea 3: Ports and adapters (swappable parts)

Imagine a wall socket. Your laptop doesn't care what's behind the wall (a power
plant, solar panels, a battery) — it just needs the socket shape. The socket is a
**port**; whatever plugs into it is an **adapter**.

Notari defines ports for the swappable jobs:
- **Extractor** — turns text into facts (a regex version *or* Claude).
- **EventStore** — stores the log (in-memory *or* Postgres).
- **Embedder** — turns text into numbers for search (a toy *or* a real model).
- **ProjectionBackend** — how reads happen (fold in memory *or* SQL on Postgres).

Because everything talks through these ports, we swapped the in-memory store for
Postgres **without changing the core logic**. In Python, a "port" is written as a
`Protocol` (an interface: "anything with these methods counts"). This pattern is
called **hexagonal architecture** or **ports and adapters**.

### Idea 4: Two kinds of time (bi-temporal)

Every fact tracks **two** timelines:
- **valid time** — when it's true *in the real world* ("lived in Delhi from 2019
  to 2026").
- **system time** — when *we learned it* ("recorded on 2026-06-27").

Keeping both lets you answer "where did they live *as of* 2020?" (valid time) and
also handle late corrections correctly. A database that tracks both is
**bi-temporal**.

### Idea 5: Provenance (receipts)

Every fact remembers **where it came from**: which original message (episode),
and even the exact snippet of text. So you can always ask "*why* do you believe
this?" and get a real answer. That receipt-keeping is **provenance**.

### Idea 6: The two trust features

These are the things competitors don't have, and they're the reason Notari exists:

- **Crypto-shred deletion.** Each user's private text is encrypted with a unique
  key. To "forget" a user, we **destroy their key** — now their encrypted data is
  unreadable forever, even though the rows still physically exist. Like shredding
  the only key to a locked box: the box remains, the contents are gone. This lets
  us obey GDPR ("delete my data") *without* breaking the never-erase event log.

- **Tamper-evident audit chain.** Each event is stamped with a hash (a fingerprint)
  that includes the previous event's hash. Change any past event and every
  fingerprint after it breaks — so you can *prove* the history wasn't altered.
  This is the same idea behind a blockchain.

---

## Part 2 — The map (where everything lives)

```
killer/
├── src/notari/        ← THE ENGINE (the Python library; 18 files)
├── clients/ts/        ← TypeScript SDK (use Notari from JavaScript)
├── examples/          ← runnable demos (start here to see it work)
├── eval/              ← measurement (tests of quality + speed + benchmarks)
├── tests/             ← automated tests (36 of them)
├── db/schema.sql      ← the Postgres database design
├── docker-compose.yml ← one command to start Postgres
├── *.md               ← docs (README, CONTRIBUTING, this file)
├── pyproject.toml     ← Python package config (name, dependencies)
└── .github/workflows/ ← CI: runs all tests automatically on every change
```

---

## Part 3 — Follow one request end to end

This is the best way to understand the engine. Here's what happens on
`mem.add("I live in Berlin", subject_id="u1")`:

1. **`memory.py`** receives the call. It hashes the text (a fingerprint), wraps the
   scope (`subject_id="u1"`), and appends an **`EpisodeIngested`** event to the store.
2. It calls the **extractor** (`extract.py`): text → `[(u1, lives_in, Berlin)]`.
3. For each fact, it checks the current projection: is there already a *different*
   `lives_in` for u1? If so, append a **`FactInvalidated`** (the old one is now
   "superseded"). Then append a **`FactAsserted`** for the new fact.
4. The **store** (`store.py` / `store_postgres.py`) saves each event — and also
   adds a hash-chain entry (`audit.py`) and, on Postgres with encryption, encrypts
   the text first (`crypto.py`).
5. The **backend** (`backend.py`) refreshes the projection.

And on `mem.search("where does the user live", subject_id="u1")`:

1. **`memory.py`** asks the **backend** to search.
2. The backend gets the **projection** (`projection.py`) — the current facts.
3. **`retrieve.py`** scores each fact against the query using **two signals**:
   *semantic* similarity (via `embed.py` — meaning) and *keyword* overlap (exact
   words), and filters by time (only facts valid "now," or "as of" a past date).
4. The top fact comes back; `answer()` returns just its value: `"Berlin"`.

`forget("u1")` appends a `SubjectForgotten` event, destroys u1's encryption key,
rebuilds the projection (u1 is gone), and returns a **certificate** proving it.

---

## Part 4 — Every file, explained

### The engine — `src/notari/`

**`events.py` — the vocabulary.** Defines the kinds of events as small, *frozen*
(unchangeable) data records: `EpisodeIngested` (raw text came in), `FactAsserted`
(we believe X), `FactInvalidated` (X is no longer current), `EntityMerged` /
`EntityUnmerged` (two names are the same thing / undo), `SubjectForgotten`. Also
`Scope` (who a memory belongs to). These events *are* the database; everything
else is derived from them. *Concept: event sourcing.*

**`store.py` — the log keeper (simple version).** Defines the `EventStore` port
(`append`, `events`, `audit_entries`) and `InMemoryEventStore`, which just keeps
events in a Python list. Tiny on purpose — a log only needs to "add" and "read
back in order." It also builds the audit hash-chain as events arrive. *Concept:
ports and adapters.*

**`embed.py` — turning words into numbers.** Computers compare meaning by turning
text into a list of numbers (a *vector*) and measuring the angle between vectors
(*cosine similarity*). `HashEmbedder` is a cheap, dependency-free stand-in;
`SentenceTransformerEmbedder` is a real AI model. Both fit the `Embedder` port.
*Concept: embeddings / vector search.*

**`extract.py` — text into facts.** `DeterministicExtractor` uses regex rules
("I live in X" → `lives_in X`) — free, no AI, used in tests. `AnthropicExtractor`
calls **Claude** to extract facts from any text (the production path). Same
`Extractor` port, two adapters. *Concept: adapters; LLM structured output.*

**`projection.py` — the fold.** Takes the event list and "folds" it into the
current picture: `Entity` nodes and `Edge` facts (each with both timelines and an
`alive` flag). `Projector.build()` walks events and applies them: assert creates
an edge, invalidate closes one, merge links names, forget drops a subject. This is
a **pure function** (same events in → same picture out), which makes it easy to
trust and test. *Concept: projections / CQRS.*

**`retrieve.py` — search.** Given the projection + a query, score each fact by
*semantic similarity* + *keyword overlap*, after filtering to the facts valid at
the requested time. Returns the best matches. *Concept: hybrid retrieval +
bi-temporal filtering.*

**`memory.py` — the front door (the SDK).** The class you actually use: `add`,
`search`, `answer`, `timeline`, `get_provenance`, `forget`, `conflicts`,
`resolve_entities`, `verify_audit`. It wires the parts together and holds the
business rules (dedup, supersession). `Memory.postgres()` is a shortcut that
plugs in the Postgres parts. *Concept: a facade — one simple interface over many
pieces.*

**`backend.py` — how reads happen.** The `ProjectionBackend` port +
`InMemoryProjectionBackend` (fold the log on every read). This is the seam that
lets the *same* `Memory` either run in memory or on Postgres. *Concept: ports.*

**`records.py` — plain return values.** `DeletionCertificate` (proof of erasure)
and `Provenance` (a fact's receipt). In their own file only to avoid "circular
imports" (two files needing each other). *Concept: keeping modules untangled.*

**`predicates.py` — single vs many.** Some facts replace each other (`lives_in`:
you live in one place) and some pile up (`uses`: you can use many tools). The
`PredicateRegistry` records which is which, so ingestion knows whether to
*supersede* or *coexist*. *Concept: domain modeling.*

**`resolver.py` — same thing, different names.** "Acme" and "Acme Corp" are the
same company. `LexicalEntityResolver` scores name pairs (shared words + spelling
closeness + meaning) and decides: auto-merge if very similar, flag for human
review if borderline, ignore if not. *Concept: entity resolution.*

**`crypto.py` — the lock and key.** `EnvelopeCipher` does **envelope encryption**:
a per-user *data key* encrypts their text; a single *master key* (KEK) encrypts
the data keys. Deleting a user's data key = their text is unrecoverable.
`NullCipher` is the "encryption off" default. *Concept: crypto-shred deletion.*

**`audit.py` — the tamper-proof seal.** Computes each event's `entry_hash` from
the previous hash + a digest of the event. `verify_entries()` re-walks the chain
to catch any edit/insert/delete. Hashes *fingerprints* (not raw text), so it stays
valid even after crypto-shred. *Concept: hash chains.*

**`store_postgres.py` — the real database adapter.** The biggest file. Implements
the same `EventStore` and `ProjectionBackend` ports against **Postgres**: saves
events to tables, encrypts text on the way in and decrypts on the way out, builds
the audit chain, **materializes** the projection into `entity`/`edge` tables, and
runs **hybrid search in SQL** using `pgvector` (vector search) + full-text search.
`forget` here destroys the key (`shred_subject`). *Concept: a production adapter.*

**`server.py` — the web API.** A **FastAPI** app exposing HTTP endpoints
(`POST /v1/add`, `GET /v1/search`, `/v1/forget/...`, `/v1/audit/verify`, etc.) so
any app — not just Python — can use Notari over the network. *Concept: REST API.*

**`console.py` — the visual UI.** A single self-contained web page (served at `/`)
that draws the memory as a graph with a time-travel slider, click-to-trace
provenance, and a forget button. *Concept: a thin frontend over the API.*

**`mcp.py` — the agent plug.** Exposes Notari as **MCP** tools (`add_memory`,
`search_memory`, `forget_subject`, …) so AI agents (like Claude) can use it
directly. *Concept: integration/distribution.*

**`__init__.py` — the public list.** Re-exports the important classes so users
write `from notari import Memory` instead of digging into files. *Concept: a
package's public API.*

### The database — `db/schema.sql`

SQL that creates the Postgres tables: `episode` and `fact_event` (the event log),
`entity`/`edge` (the materialized projection, with `vector(384)` columns for
pgvector search), `deletion_certificate`, `keyring` (the wrapped keys), and
`audit_entry` (the hash chain). Plus indexes (including an **HNSW** index that
makes vector search fast). *Concept: schema = the database's blueprint.*

### Runnable demos — `examples/`

- **`spike.py`** — the original proof: ingest → recall → time-travel → forget, with
  zero dependencies. Run this first.
- **`postgres_persistence.py`** — shows data survives a restart on Postgres.
- **`audit_and_forget_demo.py`** — the headline demo: audit → trace → forget → *prove*.
- **`verify_anthropic.py`** — proves the real Claude extractor works.

### Measurement — `eval/`

- **`harness.py`** + **`datasets.py`** — run questions through the engine and score
  accuracy (a built-in mini dataset).
- **`bench.py`** — measure retrieval *speed* (p50/p95 latency).
- **`locomo.py`** — the rough real-world benchmark slice (recall on a real
  conversation dataset).

### Automated tests — `tests/` (59 tests)

One file per feature: `test_spike.py` (core loop), `test_postgres.py`,
`test_server.py`, `test_crypto.py` + `test_crypto_shred.py`, `test_audit.py`,
`test_conflict.py`, `test_entity_resolution.py`, `test_mcp.py`. These run
automatically in CI and catch regressions. *Concept: tests = a safety net so
changes don't silently break things.*

### TypeScript SDK — `clients/ts/`

`src/index.ts` is `NotariClient` — a typed wrapper that calls the web API from
JavaScript/TypeScript. `package.json`/`tsconfig.json` configure the build.

### Config & docs

- **`pyproject.toml`** — the package's name, Python version, and optional
  dependency groups (`postgres`, `server`, `crypto`, …).
- **`docker-compose.yml`** — starts Postgres + pgvector with one command.
- **`.gitignore`** — files git should ignore (`.env` with your key, build junk).
- **`.github/workflows/ci.yml`** — runs the whole test suite on every push.
- **`LICENSE`** — Apache-2.0 (permissive open source).
- **`README` / `CONTRIBUTING`** — the pitch, and how to set up and contribute.

---

## Part 5 — Mini-glossary of the building blocks you used

- **dataclass** — a Python shortcut for a class that just holds data. `frozen=True`
  means it can't be changed after creation (good for events).
- **Protocol** — Python's way to define a port/interface: "any object with these
  methods qualifies," no inheritance needed.
- **type hints** (`str | None`, `list[float]`) — labels saying what type a value
  is. They don't run anything; they help tools and humans catch mistakes.
- **hash** (sha256) — a function turning any text into a fixed fingerprint. Same
  input → same fingerprint; you can't reverse it. Used for provenance + the audit
  chain.
- **vector / cosine similarity** — a list of numbers representing meaning; closeness
  is the angle between them.
- **idempotent / dedup** — doing the same thing twice has no extra effect (adding
  the same fact twice doesn't duplicate it).

---

## Part 6 — Learn by doing

```bash
# 1. See the whole engine work with zero setup:
python examples/spike.py

# 2. Read the code in this order (each ~one screen):
#    events.py → store.py → projection.py → retrieve.py → memory.py
#    then: crypto.py, audit.py, resolver.py, predicates.py
#    then the Postgres adapter: store_postgres.py

# 3. Start Postgres and see durability + the demos:
NOTARI_PG_PORT=5433 docker compose up -d
NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari \
    python examples/audit_and_forget_demo.py

# 4. Break a test on purpose (change an assert), run pytest, watch it fail.
python -m pytest -q
```

Suggested reading order for the *concepts*: event log → projection → ports →
bi-temporal → provenance → crypto-shred → hash chain. Once those are intuitive,
you understand the whole system.
