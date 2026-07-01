# The moat, proven

Most memory layers ask you to *trust* that a fact came from somewhere, that a
deletion happened, that history wasn't rewritten. Notari's differentiators are
**properties you can break on purpose and watch get caught** — not marketing
bullets. This page is written for a skeptic: every claim has a one-line command
and an honest note on where the property ends.

```bash
python examples/prove_the_moat.py     # zero setup: no DB, no API key, no model download
```

The script is **self-verifying**: each claim ends in an `assert`. If any property
were false, it would crash instead of printing ✅. Run it yourself.

---

## 1. Tamper-evident audit — you can't alter stored memory undetected

**The threat.** A malicious operator, a compromised database, or a subpoena-driven
"just change this one record" edits a stored fact after the fact. Can anyone tell?

**What we prove.** We seed memory, then *silently rewrite a live fact in the event
log* (`city: Berlin → Pyongyang`) while leaving the audit chain untouched — the
sneakiest possible tamper. Reads now return the forged value. Then:

```
deep verify catches it     ok=False, broken_at seq=6
delete a ledger entry      chain ok=False (linkage breaks)
```

**Why it holds.** Every event appends an `AuditEntry` whose
`entry_hash = H(prev_hash ‖ payload_hash)`, chaining to the one before it, where
`payload_hash` is a digest of the event's content (PII is hashed, never stored
raw — so the chain itself holds no personal data). Two independent checks:

- `verify_audit()` walks the links — catches any reorder, insert, or delete of the
  ledger.
- `verify_audit(deep=True)` *also* re-derives each event's digest and compares it
  to the committed `payload_hash` — catches a silent content edit that leaves the
  ledger structurally intact.

To beat both, an attacker must rewrite the entire chain from the edit forward,
which changes the head hash.

**Honest boundary.** A full rewrite of the *whole* chain is internally
self-consistent. Detecting *that* requires anchoring the head hash somewhere the
operator can't rewrite (a periodic notarization / external witness). Notari gives
you a stable head hash to anchor; publishing it is a deployment step, not
automatic. Also: `deep=True` checks content that is *present* — a sanctioned
crypto-shred (below) removes content by design, so the default chain-only verify
is what attests a shredded log.

---

## 2. Provable deletion — crypto-shred, and the content is unrecoverable

**The threat.** GDPR Art. 17 / the EU AI Act require erasing a user's data on
request. But an append-only audit log physically *keeps every row forever*. Those
two requirements look contradictory — most "delete" features just hide the row.

**What we prove.** Using the **same `EnvelopeCipher` the Postgres store uses**, we
encrypt a subject's PII under a per-subject data key (DEK), wrapped under a root
key (KEK). `forget()` destroys the wrapped DEK. The ciphertext row remains — and:

```
before forget: recoverable   True
after forget: recovery       FAILS (InvalidTag) — content unrecoverable
audit proof after forget     ok=True (proof survives erasure)
```

**Why it holds.** Without the DEK, the retained row is AES-256-GCM ciphertext —
indistinguishable from noise; recovery is infeasible. Yet the audit chain hashes
*content digests, not raw content*, so shredding the content does **not** break the
chain: `verify_audit()` still returns ok. You get erasure **and** a durable proof
it happened, including a signed `DeletionCertificate` (subject, requester,
counts, manifest hash, timestamp).

**Honest boundary.** This is *crypto-shred*, so its guarantee is "the key is
destroyed and the cipher is sound," not "the bytes were physically overwritten on
every replica" — which is exactly what makes it work across backups and replicas
you don't control. It requires encryption enabled — pass an `EnvelopeCipher` (as
the demo does) or set `NOTARI_KEK`; this works with **either** the in-memory or
the Postgres store, so the demo proves it end-to-end with no database. With no
cipher (the zero-dependency default), `forget()` is a logical delete plus
certificate — the content is dropped from all reads but not cryptographically
shredded.

---

## 3. Bi-temporal time-travel — correct without erasing; query the past

**The threat.** A user moves cities, changes employer, corrects a typo. Naive
memory overwrites the old value and the history is gone — you can't answer "what
did the system believe on date D?" for an audit or a dispute.

**What we prove.**

```
today  -> where do they live       Berlin
as_of 2022 -> where did they        Toronto
timeline retains history            ['Toronto', 'Berlin']
```

**Why it holds.** Facts carry **valid-time** (`valid_from`/`valid_to`, true-in-world)
separate from **system-time** (`recorded_at`, when Notari learned it). A correction
*supersedes* rather than deletes: the old fact's window is closed, the new one
opens, and both remain in the timeline. `search(..., as_of=D)` filters to what was
valid at instant D.

**Honest boundary.** Time-travel answers reflect what was *recorded*; it is not a
prediction of unrecorded past state. Extraction quality bounds what facts exist to
travel over (see the LOCOMO retrieval eval for that signal).

---

## Why this is the wedge

| Property | Notari | supermemory / Mem0 / Zep |
|---|---|---|
| Provable deletion + certificate (GDPR Art. 17) | ✅ crypto-shred | ✗ (auto-expiry ≠ proof) |
| Tamper-evident audit (edit/insert/delete caught) | ✅ hash chain + content check | ✗ |
| Bi-temporal "what did it know on date D?" | ✅ | partial (Zep temporal graph) |
| Runs on plain Postgres, no graph DB, vendor-neutral | ✅ | ✗ |

The retrieval-accuracy race (LOCOMO/LongMemEval) is crowded and well-funded.
*Auditable, provably deletable* memory on neutral infrastructure is the axis the
leaders don't compete on — and the one a bank, hospital, or insurer under the EU
AI Act actually has to buy. This page is the proof you can hand them.
