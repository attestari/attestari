"""`attestari` command-line entry point.

Currently exposes `attestari verify`: re-check the tamper-evident audit chain
(and, with ``--user``, a subject's erasure) against the current ledger, exiting
non-zero on any mismatch so it drops straight into CI and audit scripts.

The ledger is the zero-infra SQLite store by default (``Memory.local``); set
``ATTESTARI_DATABASE_URL`` to verify a Postgres deployment instead.
"""

from __future__ import annotations

import argparse
import os


def _open_memory():
    from .memory import Memory

    dsn = os.environ.get("ATTESTARI_DATABASE_URL")
    return Memory.postgres(dsn) if dsn else Memory.local()


def _cmd_verify(mem, *, user: str | None, deep: bool) -> int:
    """Print the verification result; return 0 if everything holds, 1 otherwise."""
    report = mem.verify_audit(deep=deep)
    label = "audit deep" if deep else "audit chain"
    if report.ok:
        print(f"{label} -> ok=True, {report.entries} entries, head {report.head[:12]}…")
    else:
        print(f"{label} -> ok=False — BROKEN at seq {report.broken_at} ({report.entries} entries)")
    ok = report.ok

    # A subject's erasure claim is only trustworthy on an intact chain, so we
    # always report the chain first, then the per-subject check on top of it.
    if user is not None:
        live = mem.timeline(subject_id=user)
        if mem.is_forgotten(user) and not live:
            print(f"subject {user!r} -> erased (forget on record; no live data remains)")
        elif mem.is_forgotten(user):
            print(f"subject {user!r} -> INCOMPLETE — forget on record but {len(live)} live fact(s) remain")
            ok = False
        else:
            print(f"subject {user!r} -> NOT erased — no forget on record for this subject")
            ok = False

    return 0 if ok else 1


def main(argv: list[str] | None = None, *, memory=None) -> int:
    parser = argparse.ArgumentParser(prog="attestari", description="Attestari command-line tools.")
    sub = parser.add_subparsers(dest="command")
    v = sub.add_parser(
        "verify",
        help="Re-check the audit chain (and optionally a subject's erasure) against the current ledger.",
    )
    v.add_argument(
        "--user", metavar="SUBJECT_ID", help="Also verify this subject's data is provably erased."
    )
    v.add_argument(
        "--deep",
        action="store_true",
        help="Deep check: re-hash stored content, not just the chain links.",
    )

    args = parser.parse_args(argv)
    if args.command != "verify":
        parser.print_help()
        return 2

    mem = memory if memory is not None else _open_memory()
    return _cmd_verify(mem, user=args.user, deep=args.deep)


if __name__ == "__main__":  # pragma: no cover - entrypoint
    raise SystemExit(main())
