#!/usr/bin/env python3
"""Rough LOCOMO slice — an INTERNAL SIGNAL, not a competitive benchmark.

Measures *evidence-retrieval recall@k*: did our memory retrieve a fact from one of
the turns LOCOMO labels as the answer's evidence? (Grounded in LOCOMO's evidence
labels; sidesteps fuzzy answer-text matching.)

Facts are extracted ONCE (a single, **paced** Claude pass) and cached to disk, so
the embedder A/B (hash vs real embeddings) runs offline with no further API calls
and no rate-limit risk.

Caveats: one conversation, turns capped for cost; retrieval recall != LOCOMO's
official answer-accuracy (LLM-judge) protocol; adversarial (cat 5) skipped; only
questions whose evidence is within the ingested turns are scored.

    set -a && source .env && set +a
    python -m eval.locomo --max-sessions 8 --turn-cap 100 --sleep 1.2 --embedders hash,st
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notari import AnthropicExtractor, HashEmbedder, Memory  # noqa: E402
from notari.events import Scope  # noqa: E402
from notari.extract import ExtractedFact  # noqa: E402


def _session_date(label: str | None, fallback_idx: int) -> str:
    if label:
        m = re.search(r"on (.+)", label)
        if m:
            for fmt in ("%d %B, %Y", "%d %B %Y"):
                try:
                    return datetime.strptime(m.group(1).strip(), fmt).date().isoformat()
                except ValueError:
                    pass
    return (datetime(2022, 1, 1, tzinfo=timezone.utc) + timedelta(days=30 * fallback_idx)).date().isoformat()


class _ReplayExtractor:
    """Returns pre-extracted facts (set per turn) — no API calls."""

    def __init__(self) -> None:
        self.current: list[ExtractedFact] = []

    def extract(self, text, scope):  # noqa: ANN001
        return self.current


class _CachedEmbedder:
    """Memoizes embeddings by text so the rebuild-on-read fold stays cheap."""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self.inner = inner
        self.dim = inner.dim
        self._cache: dict[str, list[float]] = {}

    def embed(self, text: str) -> list[float]:
        if text not in self._cache:
            self._cache[text] = self.inner.embed(text)
        return self._cache[text]


def _extract(sample: dict, max_sessions: int, turn_cap: int, model: str, sleep: float) -> list[dict]:
    conv = sample["conversation"]
    extractor = AnthropicExtractor(model=model)
    sessions = sorted(
        (k for k in conv if re.fullmatch(r"session_\d+", k)),
        key=lambda k: int(k.split("_")[1]),
    )[:max_sessions]

    out: list[dict] = []
    n = 0
    print(f"extracting up to {turn_cap} turns with {model} (paced {sleep}s/call)…")
    for si, skey in enumerate(sessions):
        date = _session_date(conv.get(f"{skey}_date_time"), si)
        for turn in conv[skey]:
            if n >= turn_cap:
                break
            facts = extractor.extract(turn["text"], Scope(subject_id=turn["speaker"]))
            out.append({
                "speaker": turn["speaker"],
                "dia_id": turn["dia_id"],
                "date": date,
                "text": turn["text"],
                "facts": [
                    {"subject": f.subject, "predicate": f.predicate,
                     "object": f.object, "confidence": f.confidence}
                    for f in facts
                ],
            })
            n += 1
            if n % 20 == 0:
                print(f"  …{n} turns")
            time.sleep(sleep)  # pace to respect rate limits
        if n >= turn_cap:
            break
    return out


def load_or_extract(sample: dict, args: argparse.Namespace) -> list[dict]:
    cache = Path(args.cache or
                 f"eval/data/facts_s{args.sample}_{args.max_sessions}_{args.turn_cap}.json")
    if cache.exists():
        print(f"using cached facts: {cache}")
        return json.loads(cache.read_text())
    turns = _extract(sample, args.max_sessions, args.turn_cap, args.model, args.sleep)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(turns))
    print(f"cached facts to {cache}")
    return turns


def evaluate(turns: list[dict], qa: list[dict], embedder, topk: int) -> tuple[int, int]:  # noqa: ANN001
    replay = _ReplayExtractor()
    mem = Memory(extractor=replay, embedder=_CachedEmbedder(embedder))
    ingested: set[str] = set()
    for t in turns:
        replay.current = [ExtractedFact(**f) for f in t["facts"]]
        mem.add(t["text"], subject_id=t["speaker"], valid_from=t["date"], source_ref=t["dia_id"])
        ingested.add(t["dia_id"])

    scorable = [
        q for q in qa
        if q.get("category") != 5 and q.get("evidence")
        and all(e in ingested for e in q["evidence"])
    ]
    hits = 0
    for q in scorable:
        results = mem.search(q["question"], limit=topk)
        dias = {
            mem.get_provenance(r.edge.fact_id).source_ref
            for r in results
            if mem.get_provenance(r.edge.fact_id)
        }
        if dias & set(q["evidence"]):
            hits += 1
    return hits, len(scorable)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="eval/data/locomo10.json")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--max-sessions", type=int, default=8)
    ap.add_argument("--turn-cap", type=int, default=100)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--embedders", default="hash,st")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--sleep", type=float, default=1.2)
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()

    sample = json.loads(Path(args.path).read_text())[args.sample]
    turns = load_or_extract(sample, args)
    n_facts = sum(len(t["facts"]) for t in turns)
    print(f"\n{len(turns)} turns, {n_facts} facts. Scoring each embedder:\n")

    for name in [e.strip() for e in args.embedders.split(",")]:
        if name == "hash":
            emb = HashEmbedder()
        elif name == "st":
            from notari import SentenceTransformerEmbedder

            emb = SentenceTransformerEmbedder()
        else:
            raise SystemExit(f"unknown embedder {name!r}")
        hits, total = evaluate(turns, sample["qa"], emb, args.topk)
        print(f"  {name:>4}  evidence-retrieval recall@{args.topk}: "
              f"{hits / max(total, 1):.2f}  ({hits}/{total})")

    print("\n(INTERNAL SIGNAL on a capped slice — not comparable to published LOCOMO "
          "answer-accuracy.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
