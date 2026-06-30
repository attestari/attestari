#!/usr/bin/env python3
"""Verify the Claude extraction path end-to-end (Haiku, low cost).

Proves the production AnthropicExtractor turns free-form text into structured,
bi-temporal facts. Needs ANTHROPIC_API_KEY in the environment.

    set -a && source .env && set +a
    python examples/verify_anthropic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notari import AnthropicExtractor, Memory  # noqa: E402

# Cheapest tier for a verification run.
mem = Memory(extractor=AnthropicExtractor(model="claude-haiku-4-5-20251001"))

mem.add(
    "Hi, my name is Dana. I live in Delhi and I work at Acme.",
    subject_id="u1",
    valid_from="2019-01-01",
)
mem.add(
    "Actually, I moved to Berlin and joined Globex last month.",
    subject_id="u1",
    valid_from="2026-03-01",
)

print("name :", mem.answer("what is the user's name", subject_id="u1"))
print("lives:", mem.answer("where does the user live", subject_id="u1"))
print("works:", mem.answer("where does the user work", subject_id="u1"))
print("lived in 2020:", mem.answer("where did the user live", subject_id="u1", as_of="2020-01-01"))
print("--- extracted facts (timeline) ---")
for e in mem.timeline(subject_id="u1"):
    print(f"  {e.predicate:<10} {e.object:<10} {'alive' if e.alive else 'superseded'}")
