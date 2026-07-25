"""Tests for the `attestari verify` CLI.

Run with `python -m pytest -q` (pyproject sets pythonpath=src). The command is
driven through `main(..., memory=...)` so the tests never touch a real on-disk
ledger.
"""

from __future__ import annotations

from attestari import Memory
from attestari.cli import main


def _seed() -> Memory:
    mem = Memory()
    mem.add("Hi, I'm Dana. I live in Delhi.", subject_id="u1", valid_from="2020-01-01")
    mem.add("I'm Ravi and I live in Chennai.", subject_id="u2", valid_from="2021-01-01")
    return mem


def test_verify_chain_ok(capsys) -> None:
    rc = main(["verify"], memory=_seed())
    out = capsys.readouterr().out
    assert rc == 0
    assert "ok=True" in out


def test_verify_user_erased_passes_and_untouched_fails(capsys) -> None:
    mem = _seed()
    mem.forget("u1")

    # A forgotten subject with no live data left -> verified erased, exit 0.
    rc = main(["verify", "--user", "u1"], memory=mem)
    out = capsys.readouterr().out
    assert rc == 0
    assert "erased" in out

    # A subject never forgotten -> not erased, exit 1 (a real audit mismatch).
    rc2 = main(["verify", "--user", "u2"], memory=mem)
    out2 = capsys.readouterr().out
    assert rc2 == 1
    assert "NOT erased" in out2


def test_verify_no_subcommand_prints_help(capsys) -> None:
    rc = main([], memory=_seed())
    assert rc == 2
