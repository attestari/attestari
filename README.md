# Notari

**The auditable memory layer for AI agents.** Give your agent long-term memory —
like any memory layer — except every fact carries a receipt (where it came from,
when), it runs on plain Postgres, the audit trail is tamper-evident, and any
user's data can be **provably deleted** with a signed certificate.

![license](https://img.shields.io/badge/license-Apache--2.0-blue)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![tests](https://img.shields.io/badge/tests-79%20passing-brightgreen)
![deps](https://img.shields.io/badge/core-zero%20dependencies-brightgreen)

```python
from notari import Memory

mem = Memory()
mem.add("Hi, I'm Alice. I live in Toronto and I work at Acme.",
        subject_id="alice", valid_from="2021-06-01")
mem.add("Update: I moved to Berlin and I now work at Globex.",
        subject_id="alice", valid_from="2026-01-01")          # supersedes Acme + Toronto

mem.answer("where does the user work", subject_id="alice")    # -> "Globex"  (latest)
mem.answer("where did the user live",  subject_id="alice",
           as_of="2022-01-01")                                # -> "Toronto" (time-travel)

cert = mem.forget("alice")   # right-to-be-forgotten -> a signed deletion certificate
```

No database, no API key, no model download required to run that — the core engine
has **zero dependencies**.

---

## Contents

- [Why it's different](#why-its-different)
- [What you get](#what-you-get)
- [Quickstart (30 seconds)](#quickstart-30-seconds)
- [The Python API](#the-python-api)
- [Run it for real](#run-it-for-real) — Postgres · server + console · MCP · TypeScript
- [REST API](#rest-api)
- [Configuration](#configuration)
- [Provable deletion + tamper-evident audit, in one demo](#provable-deletion--tamper-evident-audit-in-one-demo)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Docs & contributing](#docs--contributing)

## Why it's different

Hosted memory is a black box: you can't see where a "memory" came from, you can't
cleanly delete one user's data, and you can't *prove* the history wasn't altered.
For a bank, hospital, or insurer — and under GDPR / the EU AI Act — that's a
dealbreaker. Notari is the neutral, self-hostable layer that fixes exactly that.

| | Notari | Typical memory layer |
|---|---|---|
| Runs on plain Postgres (no graph DB) | ✅ | ✗ (needs Neo4j / a vector service) |
| Provenance on every fact | ✅ | partial |
| Bi-temporal ("what did it know on date D?") | ✅ | ✗ |
| **Provable deletion + certificate (GDPR)** | ✅ | ✗ |
| **Tamper-evident audit trail (hash chain)** | ✅ | ✗ |
| Works across model vendors | ✅ | usually locked to one |

The last two rows are the moat. **Deletion you can prove:** each user's data is
encrypted with their own key; `forget()` destroys the key, so the content is
unrecoverable — while an immutable log and a signed certificate remain as proof.
**A history you can verify:** every event is hash-linked, so `verify_audit()`
catches any edit, insert, or delete — and the proof survives deletion.

## What you get

- **Provenance on every fact** — each memory traces back to the exact source
  message, with a character span, confidence, and timestamps.
- **Bi-temporal time travel** — query memory `as_of` any past instant; corrections
  supersede old facts without erasing them, so history is always reconstructable.
- **Provable deletion** — `forget(subject_id)` crypto-shreds a user's data and
  returns a signed `DeletionCertificate`; content backups and replicas are
  covered (key storage needs its own backup policy — see
  [the threat model](docs/the-moat.md)).
- **Tamper-evident audit** — a hash-linked event chain; `verify_audit()` detects
  any edit/insert/delete, and the proof survives crypto-shred.
- **Conflict resolution** — single- vs multi-valued predicates; conflicts are
  surfaced via `conflicts()`, not silently dropped.
- **Entity resolution** — merge "the same entity, many surface forms," reversibly.
- **Hybrid retrieval** — semantic (pgvector) ⊕ keyword (full-text) ⊕ graph, with
  the bi-temporal filter built in.
- **Runs anywhere** — zero-dependency in-memory engine, or durable on one Postgres
  + pgvector container. No graph database. Works across model vendors.

## Quickstart (30 seconds)

```bash
git clone https://github.com/notarihq/notari && cd notari
python examples/spike.py              # zero-dep end-to-end loop
python examples/agent_with_memory.py  # the "give an agent memory" pattern
```

You'll see facts change over time, a bi-temporal query answer differently "as of"
different dates, a provenance trace back to the source, and a `forget()` that
issues a certificate. No install, no API key, no database.

## The Python API

One facade, `Memory`, covers the whole surface:

```python
from notari import Memory

mem = Memory()                       # zero-dep, in-memory (tests, demos)
# mem = Memory.local()               # durable in one local SQLite file — zero infrastructure
# mem = Memory.postgres()            # durable on Postgres + pgvector (production service)

# --- write -------------------------------------------------------------
fact_ids = mem.add(
    "I moved to Berlin and I now work at Globex.",
    subject_id="alice",              # whose memory this is
    valid_from="2026-01-01",         # when it became true (defaults to now)
    source_ref="chat:msg-42",        # where it came from (for provenance)
)

# --- read --------------------------------------------------------------
mem.search("where does the user work", subject_id="alice")          # ranked SearchResults
mem.answer("where does the user work", subject_id="alice")          # -> "Globex"
mem.answer("where did the user live",  subject_id="alice",
           as_of="2022-01-01")                                      # time travel: recall as-of a past date
mem.timeline(subject_id="alice")                                    # full bi-temporal history
mem.get_provenance(fact_ids[0])                                     # source episode + span
mem.conflicts(subject_id="alice")                                   # surfaced conflicts

# --- govern ------------------------------------------------------------
report = mem.verify_audit()          # AuditReport — is the hash chain intact?
cert   = mem.forget("alice")         # DeletionCertificate — provable erasure
```

| Method | Returns | What it does |
|---|---|---|
| `add(text, *, subject_id, valid_from=…, source_ref=…)` | `list[str]` | Ingest a message; extract, dedup, and supersede facts. |
| `search(query, *, subject_id, as_of=…, limit=5)` | `list[SearchResult]` | Hybrid retrieval with an optional time filter. |
| `answer(query, **kwargs)` | `str \| None` | The single top object for a query. |
| `timeline(*, subject_id)` | `list[Edge]` | Every fact for a subject, live and superseded. |
| `get_provenance(fact_id)` | `Provenance \| None` | Trace a fact to its source episode + span. |
| `conflicts(*, subject_id=None)` | `list[dict]` | Conflicts resolved by predicate cardinality. |
| `resolve_entities(names=None, *, auto=True)` | `ResolutionResult` | Merge duplicate entities (reversible). |
| `forget(subject_id)` | `DeletionCertificate` | Crypto-shred a subject; return proof. |
| `verify_audit(deep=False)` | `AuditReport` | Verify the tamper-evident hash chain; `deep=True` also catches silent edits to event content. |

## Run it for real

Three storage tiers, one engine — every guarantee (audit chain, crypto-shred,
deep verification, time travel) holds on all three:

| Tier | Storage | For | Setup |
|---|---|---|---|
| `Memory()` | in-memory | tests, demos, determinism | none |
| `Memory.local()` | one SQLite file (`~/.notari/notari.db`) | a personal agent, MCP, prototypes — durable, single-process | none (stdlib) |
| `Memory.postgres()` | Postgres + pgvector | production: concurrent access, indexed hybrid search | one container |

**Durable with zero infrastructure** (survives restarts; nothing to install or run):

```python
from notari import Memory
mem = Memory.local()      # or Memory.local("path/to/agent.db")
```

**Durable, on Postgres + pgvector** (one container, no graph DB):

```bash
NOTARI_PG_PORT=5433 docker compose up -d       # applies the schema on first boot
pip install -e ".[postgres,embeddings]"
export NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari
```

Already have a Postgres (managed or local)? The schema ships **inside the pip
package** — no clone needed:

```bash
python -m notari.initdb postgresql://user:pass@host:5432/db   # idempotent
```
```python
from notari import Memory
mem = Memory.postgres()   # durable; materialized projections + pgvector + full-text search
```

**As a REST API + visual console:**
```bash
pip install -e ".[server]"
uvicorn notari.server:app   # API at /v1/*, the memory-graph console at /
```

**As an MCP server** (any agent — Claude, frameworks — can use it) — exposes
`add_memory / search_memory / get_provenance / forget_subject` over stdio.
Register it in your MCP client's config (e.g. Claude Desktop's
`claude_desktop_config.json`); the client launches the process for you:

```json
{
  "mcpServers": {
    "notari": {
      "command": "python",
      "args": ["-m", "notari.mcp"],
      "env": {
        "NOTARI_SQLITE_PATH": "~/.notari/notari.db",
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "NOTARI_KEK": "base64-kek-here"
      }
    }
  }
}
```
Only `command`/`args` are required. Durable by default (memories go to the local
SQLite file, so they survive app restarts); the `env` block is where per-server
config lives — add `NOTARI_DATABASE_URL` to use Postgres instead of SQLite,
`ANTHROPIC_API_KEY` to upgrade extraction to Claude, `NOTARI_KEK` to enable
crypto-shred. To run it standalone (e.g. to debug): `python -m notari.mcp`.

**From TypeScript** — the TS client talks to the REST API, so **start the server
first** (see above; it defaults to `http://localhost:8000`). Then see
[`clients/ts`](clients/ts) (`@notari/client`), a thin typed client mirroring the
`Memory` surface.

**With LangChain:** see [`clients/langchain`](clients/langchain) (`notari-langchain`)
— a `NotariRetriever` (recall facts with provenance) and `NotariChatMessageHistory`
(drop-in memory for `RunnableWithMessageHistory`) for any chain or agent.

**With real Claude extraction** (instead of the zero-dep deterministic extractor):
```bash
pip install -e ".[anthropic]"
export ANTHROPIC_API_KEY=sk-ant-...
python examples/spike.py --llm anthropic
```

**Enable crypto-shred deletion** (turn `forget()` from a logical delete into
cryptographic erasure). Encryption is opt-in via a root key-encryption key
(KEK); with none set, `forget()` still works but only drops the data from reads.
Mint a KEK once and set it in the environment:
```bash
pip install -e ".[crypto]"
export NOTARI_KEK=$(python -c "from notari.crypto import generate_kek; print(generate_kek())")
```
Now each subject's PII is encrypted at rest under a per-subject key, and
`forget()` destroys that key — the ciphertext is unrecoverable, while the audit
proof survives. **Keep the KEK out of the database and its backups** (env var or
a KMS) — storing it next to the data defeats the shred. See the backup boundary
in [docs/the-moat.md](docs/the-moat.md).

**Deploying for real.** A production checklist:
- **Storage:** use `Memory.postgres()` (concurrent access); apply the schema with
  `python -m notari.initdb "$NOTARI_DATABASE_URL"`. `Memory.local()` (SQLite) is
  single-process — great for one agent or an MCP server, not a shared service.
- **Server:** run under a process manager, e.g.
  `uvicorn notari.server:app --host 0.0.0.0 --port 8000 --workers 4` behind a
  reverse proxy; put your own auth in front (the API ships without auth).
- **Extraction & embeddings:** set `ANTHROPIC_API_KEY` (extraction auto-upgrades
  to Claude) and install `[embeddings]` for real semantic vectors.
- **Keys:** inject `NOTARI_KEK` from a KMS/secrets manager as an env var — never
  bake it into an image or the DB. Back the `keyring` table up on a separate,
  short-retention policy (or rotate the KEK) so a restored data backup can't
  resurrect a shredded subject — see [docs/the-moat.md](docs/the-moat.md).
- **Secrets:** nothing is read from a `.env` file automatically — export the vars
  (or use your orchestrator's secret injection) before starting the process.

## REST API

`uvicorn notari.server:app` serves:

| Method & path | Purpose |
|---|---|
| `GET /healthz` | Liveness check. |
| `POST /v1/add` | Ingest a message. |
| `GET /v1/search` | Hybrid retrieval (`q`, `subject_id`, `as_of`, `limit`). |
| `GET /v1/timeline` | Full bi-temporal history for a subject. |
| `GET /v1/provenance/{fact_id}` | Trace a fact to its source. |
| `POST /v1/forget/{subject_id}` | Provable deletion → certificate. |
| `GET /v1/conflicts` | Surfaced conflicts. |
| `GET /v1/audit/verify` | Verify the audit hash chain. |
| `GET /v1/graph` | The memory graph (for the console). |
| `GET /` | The visual graph console. |

## Configuration

**Environment variables** (all optional — the engine runs with none of them):

| Variable | Effect |
|---|---|
| `NOTARI_DATABASE_URL` | Postgres DSN; the server/MCP use Postgres instead of local SQLite. |
| `NOTARI_SQLITE_PATH` | Where `Memory.local()`-backed server/MCP keep the SQLite file (default `~/.notari/notari.db`). |
| `NOTARI_KEK` | Root key-encryption key; turns on crypto-shred deletion. |
| `NOTARI_PG_PORT` | Host port for the bundled `docker compose` Postgres (default 5432). |
| `ANTHROPIC_API_KEY` | Enables Claude fact extraction — the server/MCP upgrade from the regex extractor automatically. |
| `NOTARI_EXTRACTOR_MODEL` | Override the extraction model (default `claude-opus-4-8`). |

**Install extras** (`pip install -e ".[extra]"`):

| Extra | Adds |
|---|---|
| `postgres` | `psycopg` + `pgvector` — the durable event store. |
| `embeddings` | `sentence-transformers` — real semantic embeddings. |
| `crypto` | `cryptography` — crypto-shred deletion. |
| `server` | FastAPI + uvicorn + MCP — the REST server and MCP server. |
| `anthropic` | The Anthropic SDK — Claude fact extraction. |
| `dev` | pytest + ruff — tests and linting. |

## Provable deletion + tamper-evident audit, in one demo

Don't trust the bullet points — **break the properties and watch them get caught.**
This runs with no database, no API key, no model download, and is self-verifying
(every claim ends in an `assert`; it crashes if any property is false):

```bash
python examples/prove_the_moat.py   # tamper -> caught · crypto-shred -> unrecoverable · time-travel
```

It adversarially proves all three differentiators: a silently rewritten fact is
**caught at the exact seq** by `verify_audit(deep=True)`; a crypto-shredded
subject's ciphertext is **provably unrecoverable** while the audit proof survives;
and a corrected fact is queryable in the past without erasing history. (Runs on a
bare clone; `pip install "notari[crypto]"` upgrades claim 2 from logical erasure to
cryptographic crypto-shred.) See
[docs/the-moat.md](docs/the-moat.md) for the threat model and honest boundaries.

For the full crypto-shred against a real encrypted Postgres row:

```bash
NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari \
    python examples/audit_and_forget_demo.py   # audit -> trace -> forget -> PROVE
```
It shows: a fact traced to its source, a subject forgotten, the raw row confirmed
to be unreadable ciphertext, recall returning nothing — and the **audit chain
still valid** after the erasure.

## How it works

The source of truth is an **append-only event log**; everything you query (the
knowledge graph, the vector index, the keyword index) is a **projection** you can
rebuild from it. That's why audit, time-travel, provenance, and provable deletion
fall out of the design instead of being bolted on.

- [ARCHITECTURE.md](ARCHITECTURE.md) — the design, end to end.
- [LEARN.md](LEARN.md) — the same ideas explained from scratch, for newcomers.

## Project layout

```
src/notari/         the engine — events, store, projection, retrieve, memory,
                    crypto (shred), audit (hash chain), predicates, resolver
src/notari/server.py, console.py   FastAPI REST API + the graph console
src/notari/mcp.py   the MCP server
examples/           runnable demos — start with spike.py
eval/               quality + retrieval-latency harness
clients/ts/         the TypeScript SDK (@notari/client)
clients/langchain/  the LangChain integration (notari-langchain)
src/notari/db/schema.sql       the Postgres bi-temporal schema
docker-compose.yml  Postgres + pgvector
```

## Docs & contributing

- [ARCHITECTURE.md](ARCHITECTURE.md) — the design
- [LEARN.md](LEARN.md) — how Notari works, from scratch
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, good first issues
- [SECURITY.md](SECURITY.md) — reporting vulnerabilities

## Status

The engine and its differentiators — verifiable deletion, tamper-evident audit,
bi-temporal provenance, Postgres-native retrieval — are **built and tested** (79
tests; Postgres p95 ≈ 1 ms).

## License

Apache-2.0. The core stays permissively licensed; the hosted cloud and
enterprise/governance features are the commercial layer.
