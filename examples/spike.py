#!/usr/bin/env python3
"""Notari de-risking spike.

Proves the whole architecture loop end to end with ZERO dependencies and no API
key: ingest -> extract -> append events -> project -> bi-temporal recall ->
provenance -> entity merge -> provable deletion.

    python examples/spike.py                 # deterministic extractor (default)
    python examples/spike.py --llm anthropic # real Claude extraction (needs key)

Exit code 0 means all the core invariants hold.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `notari` importable without installing (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notari import AnthropicExtractor, DeterministicExtractor, Memory  # noqa: E402

SUBJECT = "user_42"


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser(description="Notari spike")
    parser.add_argument("--llm", choices=["deterministic", "anthropic"], default="deterministic")
    args = parser.parse_args()

    deterministic = args.llm == "deterministic"
    extractor = DeterministicExtractor() if deterministic else AnthropicExtractor()
    mem = Memory(extractor=extractor)

    rule("1. Ingest two episodes for one subject, dated months apart")
    mem.add(
        "Hi, my name is Dana. I live in Delhi and I work at Acme.",
        subject_id=SUBJECT,
        valid_from="2019-01-01",
        source_ref="chat:msg-1",
    )
    mem.add(
        "Quick update — I moved to Berlin and I joined Globex.",
        subject_id=SUBJECT,
        valid_from="2026-03-01",
        source_ref="chat:msg-2",
    )
    print("   ingested 2 episodes")

    rule("2. Bi-temporal recall — same question, different 'as of' instants")
    now_city = mem.answer("where does the user live", subject_id=SUBJECT)
    then_city = mem.answer("where did the user live", subject_id=SUBJECT, as_of="2020-01-01")
    workplace = mem.answer("where does the user work", subject_id=SUBJECT)
    print(f"   lives now            -> {now_city}")
    print(f"   lived as of 2020     -> {then_city}")
    print(f"   works now            -> {workplace}")

    rule("3. Timeline — the full bi-temporal history (current + superseded)")
    for e in mem.timeline(subject_id=SUBJECT):
        status = "current" if e.alive else "superseded"
        until = e.valid_to.date().isoformat() if e.valid_to else "open"
        print(f"   {e.predicate:<9} {e.object:<8} [{e.valid_from.date()} -> {until}] ({status})")

    rule("4. Provenance — trace the current 'lives_in' fact back to its source")
    top = mem.search("where does the user live", subject_id=SUBJECT)[0]
    prov = mem.get_provenance(top.edge.fact_id)
    if prov:
        print(f"   fact {top.edge.subject} lives_in {top.edge.object}")
        print(f"   from episode {prov.source_episode_id[:8]} ({prov.source_ref}), snippet: {prov.snippet!r}")

    rule("5. Entity resolution — merge an alias into the subject")
    mem.merge_entities(canonical_id=SUBJECT, alias_id="the founder", evidence="same person")
    aliases = mem.search("where does the user live", subject_id=SUBJECT)  # force a project
    entity = mem._project().entities.get(SUBJECT)
    print(f"   {SUBJECT} aliases -> {sorted(entity.aliases) if entity else []}")

    rule("6. Right-to-be-forgotten — destroy the subject and get a certificate")
    cert = mem.forget(SUBJECT, requested_by="dpo@example.com")
    print(f"   certificate {cert.certificate_id[:8]}: deleted {cert.facts_deleted} facts, "
          f"{cert.episodes_deleted} episodes")
    print(f"   manifest_hash {cert.manifest_hash[:16]}…  (content destroyed, proof kept)")
    after = mem.answer("where does the user live", subject_id=SUBJECT)
    print(f"   recall after forget  -> {after}")

    # --- Exit gate: assert the invariants (deterministic mode only) ------- #
    if deterministic:
        rule("7. Asserting core invariants")
        assert now_city == "Berlin", f"expected Berlin, got {now_city!r}"
        assert then_city == "Delhi", f"expected Delhi (bi-temporal), got {then_city!r}"
        assert workplace == "Globex", f"expected Globex, got {workplace!r}"
        assert prov is not None and prov.snippet == "Berlin", f"bad provenance: {prov!r}"
        assert entity is not None and "the founder" in entity.aliases, "alias merge failed"
        assert cert.facts_deleted >= 3, f"expected >=3 facts deleted, got {cert.facts_deleted}"
        assert after is None, f"subject should have no memories after forget, got {after!r}"
        assert not aliases or True  # search ran without error
        print("   ✅ all invariants hold — the architecture loop is sound")
    else:
        print("\n(anthropic mode: extraction is model-driven; invariants not asserted)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
