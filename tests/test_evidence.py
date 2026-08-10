"""Tests for the evidence bundle and the `attestari evidence` CLI.

The bundle is the auditor-facing artifact (see `auditor/`), so what matters here
is that it re-derives its claims from the live ledger rather than reporting
whatever it was told: a broken chain, or an erasure that didn't fully take, must
surface as a FAIL.
"""

from __future__ import annotations

import dataclasses
import json

from attestari import Memory
from attestari.cli import main
from attestari.events import FactAsserted
from attestari.evidence import build_evidence, render_markdown


def _tamper(mem: Memory) -> None:
    """Rewrite a stored fact in place, leaving the audit chain untouched — the
    sneaky tamper only deep verification catches (see tests/test_audit.py)."""
    log = mem.store._log
    idx = next(i for i, e in enumerate(log) if isinstance(e, FactAsserted))
    log[idx] = dataclasses.replace(log[idx], object="Pyongyang")


def _seed() -> Memory:
    mem = Memory()
    mem.add("Hi, I'm Dana. I live in Delhi.", subject_id="u1", valid_from="2020-01-01")
    mem.add("I'm Ravi and I live in Chennai.", subject_id="u2", valid_from="2021-01-01")
    return mem


def test_clean_ledger_passes_with_no_erasures() -> None:
    bundle = build_evidence(_seed())
    assert bundle["ok"] is True
    assert bundle["verification"]["ok"] is True
    assert bundle["verification"]["entries"] > 0
    assert len(bundle["verification"]["head"]) == 64
    assert bundle["erasures"] == []


def test_erasure_appears_in_register_and_is_rechecked() -> None:
    mem = _seed()
    mem.forget("u1", requested_by="dpo@example.com")
    bundle = build_evidence(mem, deep=True)

    assert bundle["ok"] is True
    assert [row["subject_id"] for row in bundle["erasures"]] == ["u1"]
    row = bundle["erasures"][0]
    assert row["requested_by"] == "dpo@example.com"
    assert row["erased"] is True
    assert row["live_facts"] == 0
    # The other subject is untouched by an erasure of u1.
    assert mem.timeline(subject_id="u2")


def test_repeated_forget_collapses_to_one_register_row() -> None:
    mem = _seed()
    mem.forget("u1")
    mem.forget("u1")
    bundle = build_evidence(mem)
    assert len(bundle["erasures"]) == 1


def test_tampered_chain_fails_the_bundle() -> None:
    mem = _seed()
    bundle_before = build_evidence(mem, deep=True)
    assert bundle_before["ok"] is True

    _tamper(mem)

    bundle = build_evidence(mem, deep=True)
    assert bundle["verification"]["ok"] is False
    assert bundle["verification"]["broken_at"] is not None
    assert bundle["ok"] is False
    assert "BROKEN" in render_markdown(bundle)


def test_in_memory_tier_reports_no_certificate_register() -> None:
    # The tier keeps no certificates: report that honestly rather than implying
    # an empty history (null, not []).
    mem = _seed()
    mem.forget("u1")
    assert build_evidence(mem)["certificates"] is None


def test_markdown_renders_the_register_and_reproduction_steps() -> None:
    mem = _seed()
    mem.forget("u1", requested_by="dpo@example.com")
    md = render_markdown(build_evidence(mem, deep=True))

    assert "**PASS**" in md
    assert "dpo@example.com" in md
    assert "erased — no live data" in md
    assert "attestari verify --deep" in md  # the reader can re-derive it


def test_cli_writes_both_files_and_exits_zero(tmp_path, capsys) -> None:
    out = tmp_path / "bundle"
    rc = main(["evidence", "--deep", "--out", str(out)], memory=_seed())
    assert rc == 0

    written = capsys.readouterr().out
    assert "evidence.json" in written

    payload = json.loads((out / "evidence.json").read_text())
    assert payload["ok"] is True
    assert payload["verification"]["mode"] == "deep"
    assert (out / "EVIDENCE.md").read_text().startswith("# Attestari evidence bundle")


def test_cli_exits_non_zero_when_a_claim_fails(tmp_path) -> None:
    mem = _seed()
    _tamper(mem)

    # A failing bundle is still written (the auditor needs to see *what* failed),
    # but the exit code makes it usable as a scheduled control. Deep mode is
    # what catches an in-place content rewrite.
    rc = main(["evidence", "--deep", "--out", str(tmp_path)], memory=mem)
    assert rc == 1
    assert (tmp_path / "EVIDENCE.md").exists()
