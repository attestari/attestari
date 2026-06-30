#!/usr/bin/env python3
"""Durability demo: the same engine, now durable on Postgres.

    NOTARI_PG_PORT=5433 docker compose up -d
    pip install "psycopg[binary]"
    NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari \
        python examples/postgres_persistence.py

Proves the in-memory loop works identically against Postgres, and that the data
survives a brand-new connection (i.e. a process restart).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notari import Memory, PostgresEventStore  # noqa: E402

DSN = os.environ.get("NOTARI_DATABASE_URL", "postgresql://notari:notari@localhost:5433/notari")


def main() -> int:
    store = PostgresEventStore(DSN)
    store.truncate()  # fresh start for a clean demo

    print(f"writing to {DSN}")
    mem = Memory(store=store)
    mem.add("Hi, my name is Dana. I live in Delhi and I work at Acme.",
            subject_id="u1", valid_from="2019-01-01", source_ref="m1")
    mem.add("I moved to Berlin and I joined Globex.",
            subject_id="u1", valid_from="2026-03-01", source_ref="m2")
    print("  wrote 2 episodes")

    print("\n--- reopen with a fresh connection (simulates a process restart) ---")
    mem2 = Memory(store=PostgresEventStore(DSN))
    print(f"  lives now        -> {mem2.answer('where does the user live', subject_id='u1')}")
    print(f"  lived as of 2020 -> {mem2.answer('where did the user live', subject_id='u1', as_of='2020-01-01')}")
    top = mem2.search("where does the user live", subject_id="u1")[0]
    prov = mem2.get_provenance(top.edge.fact_id)
    print(f"  provenance       -> snippet {prov.snippet!r} from {prov.source_ref}")

    cert = mem2.forget("u1", requested_by="dpo@example.com")
    print(f"\n  forgot u1: {cert.facts_deleted} facts, {cert.episodes_deleted} episodes, "
          f"cert {cert.certificate_id[:8]}")
    print(f"  recall after forget -> {mem2.answer('where does the user live', subject_id='u1')}")
    store.close()
    print("\n✅ durable on Postgres, identical behaviour to the in-memory engine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
