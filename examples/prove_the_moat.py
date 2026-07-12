#!/usr/bin/env python3
"""Prove the moat — adversarially, with zero setup.

Attestari's differentiators aren't "trust us" bullet points; they're properties you
can *break on purpose and watch get caught*. This script does exactly that for
the three claims competitors don't make:

    1. Tamper-evident audit  — secretly rewrite a stored fact; the ledger rats it out.
    2. Provable deletion      — crypto-shred a subject; the ciphertext is unrecoverable,
                                yet the audit proof survives.
    3. Bi-temporal time-travel — correct a fact without erasing history; query the past.

Every claim ends in an `assert`. The asserts ARE the proof: if any property were
false, this script would crash instead of printing ✅. No database, no API key,
no model download — just:

    python examples/prove_the_moat.py
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from attestari import (  # noqa: E402
    EnvelopeCipher,
    InMemoryEventStore,
    Memory,
    generate_kek,
    verify_certificate,
)
from attestari.events import EpisodeIngested, FactAsserted  # noqa: E402

import base64  # noqa: E402


def rule(n: int, t: str) -> None:
    print(f"\n\033[1m{n}. {t}\033[0m")


def kv(label: str, value: object) -> None:
    print(f"   {label:<34} {value}")


# --------------------------------------------------------------------------- #
# Claim 1 — tamper-evident audit: you cannot alter stored memory undetected.
# --------------------------------------------------------------------------- #
def prove_tamper_evident() -> bool:
    rule(1, "Tamper-evident audit — secretly edit a fact, get caught")
    mem = Memory()
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I moved to Berlin.", subject_id="u1", valid_from="2026-03-01")

    clean = mem.verify_audit(deep=True)
    kv("clean chain verifies", f"ok={clean.ok}, entries={clean.entries}")
    assert clean.ok

    # THE ATTACK: silently rewrite a stored fact's object in the event log,
    # leaving the (separate) audit chain untouched — the way a malicious
    # operator or a compromised DB write would try to do it.
    log = mem.store._log
    idx = next(i for i, e in enumerate(log)
               if isinstance(e, FactAsserted) and e.object == "Berlin")  # the live city
    original = log[idx].object
    log[idx] = dataclasses.replace(log[idx], object="Pyongyang")
    kv("attacker rewrote live fact", f"city {original!r} -> {log[idx].object!r}")
    kv("...and reads now lie", f"answer('where do they live') -> "
       f"{mem.answer('where does the user live', subject_id='u1')!r}")

    caught = mem.verify_audit(deep=True)
    kv("deep verify catches it", f"ok={caught.ok}, broken_at seq={caught.broken_at}")
    assert caught.ok is False and caught.broken_at == idx + 1

    # THE ATTACK, part 2: try to cover tracks by deleting an audit entry.
    del mem.store._audit[0]
    broken = mem.verify_audit()
    kv("delete a ledger entry", f"chain ok={broken.ok} (linkage breaks)")
    assert broken.ok is False

    print("   \033[32m✅ stored memory cannot be altered without detection\033[0m")
    return True


# --------------------------------------------------------------------------- #
# Claim 2 — provable deletion: crypto-shred makes content unrecoverable while
# the immutable audit proof remains. Uses the SAME EnvelopeCipher the Postgres
# store uses, so this is the real mechanism, not a mock.
# --------------------------------------------------------------------------- #
def _have_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def prove_provable_deletion() -> bool:
    rule(2, "Provable deletion — crypto-shred, then fail to recover the content")

    # An append-only log can't truly 'forget' — the row physically remains. So
    # Attestari encrypts each subject's PII under a per-subject key and, to forget a
    # subject, DESTROYS that key. With the `crypto` extra we prove cryptographic
    # unrecoverability directly; without it we still prove logical erasure +
    # certificate + audit survival (the zero-dependency default).
    kek = None
    if _have_crypto():
        kek = generate_kek()
        store = InMemoryEventStore(cipher=EnvelopeCipher(base64.b64decode(kek)))
        mem = Memory(store=store)
    else:
        kv("note", "cryptography not installed — proving logical deletion")
        kv("", "pip install 'attestari[crypto]' for cryptographic crypto-shred")
        store = None
        mem = Memory()

    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2", valid_from="2021-01-01")

    if store is not None:
        raw = next(e.payload for e in store._log
                   if isinstance(e, EpisodeIngested) and e.scope.subject_id == "u1")
        kv("PII at rest is ciphertext", raw[:40] + "…")
        assert "Delhi" not in raw and "Dana" not in raw
    kv("before forget: recall", mem.answer("where does the user live", subject_id="u1"))
    assert mem.answer("where does the user live", subject_id="u1") == "Delhi"

    # forget(): destroy the subject and issue a certificate — signed under a
    # KEK-derived key when encryption is on, so it can't be forged or altered.
    cert = mem.forget("u1", requested_by="dpo@example.com")
    kv("deletion certificate", f"{cert.certificate_id[:8]} · {cert.facts_deleted} facts · "
       f"manifest {cert.manifest_hash[:12]}…")

    if kek is not None:
        import dataclasses

        assert cert.signature is not None
        kv("certificate is signed", f"{cert.algorithm} · sig {cert.signature[:16]}…")
        assert verify_certificate(cert, kek)
        kv("signature verifies", "verify_certificate(cert, KEK) = True")
        forged = dataclasses.replace(cert, facts_deleted=cert.facts_deleted + 40)
        assert not verify_certificate(forged, kek)
        kv("forged copy rejected", "claiming 40 extra deletions -> verification FAILS")

    if store is not None:
        # The ciphertext row physically remains, but with no key it is AES-256-GCM
        # noise — unrecoverable.
        still_there = next(e.payload for e in store._log
                           if isinstance(e, EpisodeIngested) and e.scope.subject_id == "u1")
        kv("key destroyed", f"'u1' in keyring = {'u1' in store._keyring}")
        kv("row remains, unreadable", still_there[:40] + "…")
        assert "u1" not in store._keyring and "Delhi" not in still_there

    kv("recall after forget (u1)", mem.answer("where does the user live", subject_id="u1"))
    kv("bystander u2 untouched", mem.answer("where does the user live", subject_id="u2"))
    survived = mem.verify_audit()
    kv("audit proof after forget", f"ok={survived.ok} (proof survives erasure)")

    assert mem.answer("where does the user live", subject_id="u1") is None
    assert mem.timeline(subject_id="u1") == []
    assert mem.answer("where does the user live", subject_id="u2") == "Chennai"
    assert survived.ok

    claim = ("content is provably unrecoverable; the proof remains" if store is not None
             else "subject erased from all reads + certificate + audit survives")
    print(f"   \033[32m✅ {claim}\033[0m")
    return True


# --------------------------------------------------------------------------- #
# Claim 3 — bi-temporal time-travel: correct a fact without destroying history.
# --------------------------------------------------------------------------- #
def prove_time_travel() -> bool:
    rule(3, "Bi-temporal time-travel — correct without erasing, query the past")
    mem = Memory()
    mem.add("I live in Toronto and I work at Acme.", subject_id="a1", valid_from="2021-06-01")
    mem.add("I moved to Berlin and I now work at Globex.", subject_id="a1", valid_from="2026-01-01")

    now = mem.answer("where does the user live", subject_id="a1")
    past = mem.answer("where did the user live", subject_id="a1", as_of="2022-01-01")
    kv("today  -> where do they live", now)
    kv("as_of 2022 -> where did they", past)
    assert now == "Toronto" or now == "Berlin"  # extractor-dependent surface; assert below
    assert now != past  # the point: the same query answers differently through time
    assert past == "Toronto" and now == "Berlin"

    # History is preserved, not overwritten: both values live in the timeline.
    cities = [e.object for e in mem.timeline(subject_id="a1") if e.predicate in ("city", "lives_in", "location")]
    kv("timeline retains history", cities)
    assert "Toronto" in cities and "Berlin" in cities

    print("   \033[32m✅ corrections supersede; the past stays reconstructable\033[0m")
    return True


def main() -> int:
    print("\033[1mAttestari — prove the moat (zero setup, self-verifying)\033[0m")
    ok = True
    for prove in (prove_tamper_evident, prove_provable_deletion, prove_time_travel):
        try:
            ok = prove() and ok
        except AssertionError as e:
            print(f"   \033[31m❌ claim FAILED: {e!r}\033[0m")
            ok = False
    # NB: no backslash escapes inside the f-string expression — that's 3.12+
    # syntax, and this demo must run on bare Python 3.11 (the README's floor).
    verdict = "\033[32m✅ all three moat properties proven" if ok else "\033[31m❌ a claim did not hold"
    print(f"\n{verdict}\033[0m")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
