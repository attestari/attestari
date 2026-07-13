#!/usr/bin/env python3
"""Audit & forget demo: audit a fact -> trace its source -> forget -> PROVE.

Runs against Postgres with crypto-shred when ATTESTARI_DATABASE_URL is set (a KEK is
generated if none is present), otherwise the in-memory engine.

    ATTESTARI_PG_PORT=5433 docker compose up -d
    ATTESTARI_DATABASE_URL=postgresql://attestari:attestari@localhost:5433/attestari \
        python examples/audit_and_forget_demo.py

Set ATTESTARI_DEMO_PACE=1 to reveal the beats one at a time (for recording a GIF);
leave it unset for instant output in CI and dry-runs.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from attestari import Memory, generate_kek  # noqa: E402

SUBJECT = "user_42"


def _pace_seconds() -> float:
    """Pause between beats, for recording a paced GIF; 0 (instant) unless
    ATTESTARI_DEMO_PACE is set. on/true/1 -> a default cadence; a number ->
    that many seconds. Unset keeps CI and dry-runs instant."""
    v = os.environ.get("ATTESTARI_DEMO_PACE", "").strip().lower()
    if not v or v in ("0", "off", "false"):
        return 0.0
    if v in ("1", "on", "true", "yes"):
        return 0.8
    try:
        return float(v)  # explicit cadence, e.g. ATTESTARI_DEMO_PACE=0.5
    except ValueError:
        return 0.8


PACE = _pace_seconds()


def pause() -> None:
    if PACE:
        time.sleep(PACE)


def rule(t: str) -> None:
    pause()
    print(f"\n\033[1m{t}\033[0m")


def main() -> int:
    pg = bool(os.environ.get("ATTESTARI_DATABASE_URL"))
    if pg and not os.environ.get("ATTESTARI_KEK"):
        os.environ["ATTESTARI_KEK"] = generate_kek()  # enable crypto-shred for the demo
    crypto = pg and bool(os.environ.get("ATTESTARI_KEK"))

    if pg:
        from attestari import PostgresEventStore

        PostgresEventStore().truncate()
        mem = Memory.postgres()
    else:
        mem = Memory()
    print(f"engine={'postgres' if pg else 'memory'}  crypto_shred={'on' if crypto else 'off'}")

    rule("1. Ingest")
    mem.add("Hi, my name is Dana. I live in Delhi and I work at Acme.",
            subject_id=SUBJECT, valid_from="2019-01-01", source_ref="chat:1")
    mem.add("I moved to Berlin and I joined Globex.",
            subject_id=SUBJECT, valid_from="2026-03-01", source_ref="chat:2")
    mem.add("I use Python and I use Rust.", subject_id=SUBJECT, valid_from="2026-03-02")
    print("   3 episodes ingested")

    rule("2. Trace provenance — why does it believe this?")
    top = mem.search("where does the user live", subject_id=SUBJECT)[0]
    prov = mem.get_provenance(top.edge.fact_id)
    print(f"   '{top.edge.subject} {top.edge.predicate} {top.edge.object}'"
          f"  <- {prov.source_ref}, snippet {prov.snippet!r}")

    rule("3. Conflicts — surfaced, resolved by recency")
    for c in mem.conflicts(subject_id=SUBJECT):
        vals = " -> ".join(f"{v['object']}{'*' if v['alive'] else ''}" for v in c["values"])
        print(f"   {c['predicate']}: {vals}   (resolution: {c['resolution']})")

    rule("4. Entity resolution — same entity, many names")
    res = mem.resolve_entities(["Acme", "Acme Corp", "Globex", "Globex Inc"], auto=True)
    for d in res.auto_merges:
        print(f"   merge {d.alias!r} -> {d.canonical!r}  (score {d.score})")

    rule("5. Audit chain intact?")
    report = mem.verify_audit()
    print(f"   verify_audit -> ok={report.ok}, {report.entries} entries")

    rule("6. Forget the subject — and PROVE it")
    cert = mem.forget(SUBJECT, requested_by="dpo@example.com")
    pause()
    print(f"   certificate {cert.certificate_id[:8]}: {cert.facts_deleted} facts, "
          f"{cert.episodes_deleted} episodes, manifest {cert.manifest_hash[:12]}…")
    if cert.signature:
        from attestari import verify_certificate

        verified = verify_certificate(cert, os.environ["ATTESTARI_KEK"])
        pause()
        print(f"   certificate signed    -> {cert.algorithm}, "
              f"verify_certificate = {verified}")
    pause()
    print(f"   recall after forget   -> {mem.answer('where does the user live', subject_id=SUBJECT)}")
    if crypto:
        conn = mem.store._conn
        kr = conn.execute("SELECT count(*) AS n FROM keyring WHERE subject_id=%s", (SUBJECT,)).fetchone()["n"]
        raw = conn.execute("SELECT payload FROM episode WHERE subject_id=%s LIMIT 1", (SUBJECT,)).fetchone()
        pause()
        print(f"   key destroyed         -> keyring rows for subject = {kr}")
        if raw:
            pause()
            print(f"   raw row still present -> payload is ciphertext: {'Delhi' not in raw['payload']}")
    after = mem.verify_audit()
    pause()
    print(f"   audit chain after shred -> ok={after.ok} (the proof survives erasure)")

    ok = (
        mem.answer("where does the user live", subject_id=SUBJECT) is None
        and after.ok
        and (not crypto or kr == 0)
    )
    pause()
    print(f"\n{'✅' if ok else '❌'} audit -> trace -> forget -> prove")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
