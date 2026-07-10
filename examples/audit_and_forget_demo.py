#!/usr/bin/env python3
"""Audit & forget demo: audit a fact -> trace its source -> forget -> PROVE.

Runs against Postgres with crypto-shred when NOTARI_DATABASE_URL is set (a KEK is
generated if none is present), otherwise the in-memory engine.

    NOTARI_PG_PORT=5433 docker compose up -d
    NOTARI_DATABASE_URL=postgresql://notari:notari@localhost:5433/notari \
        python examples/audit_and_forget_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notari import Memory, generate_kek  # noqa: E402

SUBJECT = "user_42"


def rule(t: str) -> None:
    print(f"\n\033[1m{t}\033[0m")


def main() -> int:
    pg = bool(os.environ.get("NOTARI_DATABASE_URL"))
    if pg and not os.environ.get("NOTARI_KEK"):
        os.environ["NOTARI_KEK"] = generate_kek()  # enable crypto-shred for the demo
    crypto = pg and bool(os.environ.get("NOTARI_KEK"))

    if pg:
        from notari import PostgresEventStore

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
    print(f"   verify_audit -> {mem.verify_audit()}")

    rule("6. Forget the subject — and PROVE it")
    cert = mem.forget(SUBJECT, requested_by="dpo@example.com")
    print(f"   certificate {cert.certificate_id[:8]}: {cert.facts_deleted} facts, "
          f"{cert.episodes_deleted} episodes, manifest {cert.manifest_hash[:12]}…")
    if cert.signature:
        from notari import verify_certificate

        verified = verify_certificate(cert, os.environ["NOTARI_KEK"])
        print(f"   certificate signed    -> {cert.algorithm}, "
              f"verify_certificate = {verified}")
    print(f"   recall after forget   -> {mem.answer('where does the user live', subject_id=SUBJECT)}")
    if crypto:
        conn = mem.store._conn
        kr = conn.execute("SELECT count(*) AS n FROM keyring WHERE subject_id=%s", (SUBJECT,)).fetchone()["n"]
        raw = conn.execute("SELECT payload FROM episode WHERE subject_id=%s LIMIT 1", (SUBJECT,)).fetchone()
        print(f"   key destroyed         -> keyring rows for subject = {kr}")
        if raw:
            print(f"   raw row still present -> payload is ciphertext: {'Delhi' not in raw['payload']}")
    after = mem.verify_audit()
    print(f"   audit chain after shred -> ok={after.ok} (the proof survives erasure)")

    ok = (
        mem.answer("where does the user live", subject_id=SUBJECT) is None
        and after.ok
        and (not crypto or kr == 0)
    )
    print(f"\n{'✅' if ok else '❌'} audit -> trace -> forget -> prove")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
