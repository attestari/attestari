"""Eval harness — record a quality number from day one.

Ingests a dataset's episodes into a fresh Memory, asks each question, and scores
the answer by case-insensitive substring match. Prints per-question results and
an accuracy figure, and (optionally) fails if accuracy drops below a floor — so
it doubles as a regression gate in CI.

    python -m eval.harness                       # built-in dataset, deterministic
    python -m eval.harness --dataset builtin --min-accuracy 1.0
    python -m eval.harness --dataset-file locomo.json --llm anthropic --engine postgres
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notari import AnthropicExtractor, DeterministicExtractor, Memory  # noqa: E402

from . import datasets  # noqa: E402


def run(
    dataset_name: str,
    llm: str,
    engine: str = "memory",
    dataset_file: str | None = None,
) -> float:
    ds = datasets.load_file(dataset_file) if dataset_file else datasets.load(dataset_name)
    extractor = DeterministicExtractor() if llm == "deterministic" else AnthropicExtractor()

    if engine == "postgres":
        from notari import PostgresEventStore

        reset = PostgresEventStore()
        reset.truncate()
        reset.close()
        mem = Memory.postgres(extractor=extractor)
    else:
        mem = Memory(extractor=extractor)

    for ep in ds.episodes:
        mem.add(ep.text, subject_id=ep.subject_id, valid_from=ep.valid_from)

    correct = 0
    print(f"\ndataset={ds.name}  llm={llm}  questions={len(ds.questions)}\n")
    for q in ds.questions:
        got = mem.answer(q.query, subject_id=q.subject_id, as_of=q.as_of)
        ok = got is not None and q.expected.lower() in got.lower()
        correct += ok
        asof = f" @{q.as_of}" if q.as_of else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {q.query}{asof} -> {got!r} (want {q.expected!r})")

    accuracy = correct / len(ds.questions) if ds.questions else 0.0
    print(f"\naccuracy: {accuracy:.3f}  ({correct}/{len(ds.questions)})")
    return accuracy


def main() -> int:
    parser = argparse.ArgumentParser(description="Notari eval harness")
    parser.add_argument("--dataset", default="builtin")
    parser.add_argument("--dataset-file", default=None, help="JSON dataset file (overrides --dataset)")
    parser.add_argument("--llm", choices=["deterministic", "anthropic"], default="deterministic")
    parser.add_argument("--engine", choices=["memory", "postgres"], default="memory")
    parser.add_argument("--min-accuracy", type=float, default=1.0)
    args = parser.parse_args()

    accuracy = run(args.dataset, args.llm, engine=args.engine, dataset_file=args.dataset_file)
    if accuracy < args.min_accuracy:
        print(f"\nFAIL: accuracy {accuracy:.3f} < required {args.min_accuracy:.3f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
