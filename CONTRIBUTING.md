# Contributing to Notari

Thanks for your interest! Notari is the **auditable memory layer for AI agents** —
event-sourced, bi-temporal, with provable deletion and a tamper-evident audit
chain. New to the codebase? Read [LEARN.md](LEARN.md) — it explains the whole
design from scratch.

## Dev setup

```bash
git clone https://github.com/notarihq/notari && cd notari
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,postgres,server,crypto,embeddings]"

# Postgres (for the durable-store tests) — optional:
NOTARI_PG_PORT=5433 docker compose up -d
export NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari
```

## Run the checks

```bash
python examples/spike.py        # the zero-dependency end-to-end loop
python -m pytest -q             # the test suite
python -m eval.harness          # quality baseline
python -m eval.bench --engine memory   # retrieval latency
```

The de-risking spike runs with **zero dependencies** — keep it that way (the core
engine must never require Postgres, an LLM, or a model download).

## The shape of the code

Everything swappable is a **port** (a `Protocol`) with adapters:
`Extractor`, `EventStore`, `Embedder`, `ProjectionBackend`. The cleanest
contributions add a new adapter without touching the core. Examples:

## Good first issues

- **`OllamaExtractor`** — a local-model fact extractor (mirror `AnthropicExtractor`).
- **OpenAI / LlamaIndex integrations** — like `clients/langchain`.
- **True BM25** — swap Postgres `ts_rank` for `pg_search`/ParadeDB in `store_postgres`.
- **Expose more on the MCP tools** — `session_id`, `as_of` on `search_memory`.
- **Incremental projection** — update `entity`/`edge` per event instead of the
  current rebuild-on-write (the big perf win).

## Pull requests

1. Branch off `main`.
2. Add/keep tests green (`pytest`) and the spike passing; CI must be green.
3. Keep the zero-dependency guarantee intact.
4. Clear commit messages; small, focused PRs.

## Code style

Python 3.11+, type hints, `ruff` for lint/format (`line-length = 100`). Match the
surrounding code's comment density and naming.

By contributing you agree your work is licensed under **Apache-2.0**.
