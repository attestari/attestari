#!/usr/bin/env python3
"""Give any AI agent long-term memory in a few lines — runs with zero setup.

The pattern: after each user turn, `remember()` it; before the agent answers,
`recall()` the relevant facts and drop them into your prompt. Attestari handles
extraction, supersession (old facts get replaced), time-travel, and deletion.

    python examples/agent_with_memory.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from attestari import Memory  # noqa: E402

mem = Memory()  # in-memory; swap for Memory.postgres() in production
USER = "alice"


def remember(text: str) -> None:
    """Call this on each user message — Attestari extracts and stores the facts."""
    mem.add(text, subject_id=USER)


def recall(query: str) -> list[str]:
    """Call this before the agent answers — returns context to put in the prompt."""
    return [f"{r.edge.subject} {r.edge.predicate} {r.edge.object}"
            for r in mem.search(query, subject_id=USER, limit=3)]


def main() -> int:
    # --- a multi-turn conversation, days apart ---
    remember("Hi! I'm Alice. I live in Toronto and I work at Acme.")
    remember("Update: I switched jobs — I now work at Globex.")  # supersedes Acme

    # --- later, the agent needs context to answer a question ---
    print("user asks: 'where do I work, and where do I live?'")
    context = recall("where does the user work and live")
    print("  memory recalled:", context)
    print("  → drop those lines into your LLM prompt as context.\n")

    # The current answer reflects the *latest* facts (Globex, not Acme):
    print("works:", mem.answer("where does the user work", subject_id=USER))   # Globex
    print("lives:", mem.answer("where does the user live", subject_id=USER))   # Toronto

    # --- right to be forgotten, with a certificate ---
    cert = mem.forget(USER)
    print(f"\nforgot {USER}: {cert.facts_deleted} facts deleted, "
          f"certificate {cert.certificate_id[:8]}")
    print("recall after forget:", recall("where does the user work"))         # []
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
