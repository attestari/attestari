"""Retrieval latency benchmark ("p95 retrieval under target").

Ingests N subjects, runs Q searches, and reports p50/p95 retrieval latency.
Fails (exit 1) if p95 exceeds --target-ms.

    python -m eval.bench --engine memory
    NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari \
        python -m eval.bench --engine postgres --subjects 50 --queries 200
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notari import Memory  # noqa: E402


def _build(engine: str, subjects: int) -> Memory:
    if engine == "postgres":
        from notari import PostgresEventStore

        reset = PostgresEventStore()
        reset.truncate()
        reset.close()
        mem = Memory.postgres()
    else:
        mem = Memory()

    for i in range(subjects):
        mem.add(
            f"My name is User{i}. I live in City{i} and I work at Org{i}.",
            subject_id=f"u{i}",
            valid_from="2020-01-01",
        )
    return mem


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered))) - 1))
    return ordered[k]


def main() -> int:
    ap = argparse.ArgumentParser(description="Notari retrieval latency benchmark")
    ap.add_argument("--engine", choices=["memory", "postgres"], default="memory")
    ap.add_argument("--subjects", type=int, default=200)
    ap.add_argument("--queries", type=int, default=500)
    ap.add_argument("--target-ms", type=float, default=200.0)
    args = ap.parse_args()

    if args.engine == "postgres":
        args.subjects = min(args.subjects, 60)  # rebuild-on-write is O(n) per add

    mem = _build(args.engine, args.subjects)

    latencies: list[float] = []
    for i in range(args.queries):
        subj = f"u{i % args.subjects}"
        t0 = time.perf_counter()
        mem.search("where does the user live", subject_id=subj, limit=5)
        latencies.append((time.perf_counter() - t0) * 1000.0)

    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    print(
        f"engine={args.engine} subjects={args.subjects} queries={args.queries}  "
        f"p50={p50:.2f}ms  p95={p95:.2f}ms  target={args.target_ms:.0f}ms"
    )
    if p95 > args.target_ms:
        print(f"FAIL: p95 {p95:.2f}ms exceeds target {args.target_ms:.0f}ms")
        return 1
    print("PASS: p95 within target")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
