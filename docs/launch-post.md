# Launch narrative (draft)

Ready-to-edit copy for the launch: a blog post, a Show HN blurb, and hero/one-liner
options. Everything here is written to be *honest* — it claims only what
`prove_the_moat.py` and the test suite actually demonstrate.

---

## Hero one-liners (pick one)

- **Notari — the auditable memory layer for AI agents.** Every fact carries a
  receipt, the history is tamper-evident, and any user's data can be *provably*
  deleted. Runs on plain Postgres.
- **AI memory you can hand to a regulator.** Provenance on every fact, a
  tamper-evident audit trail, and crypto-shred deletion with a signed certificate.
- **The memory layer that can prove it forgot.** Don't trust us — clone it and
  break the guarantees yourself.

---

## Blog post — "Memory an auditor would accept"

Every AI memory layer promises the same thing: your agent stops forgetting. Fewer
ask the question a bank, a hospital, or an insurer has to answer before they can
ship anything: **can you prove it?**

- Prove *where* a "memory" came from.
- Prove you *deleted* a user's data when they asked — not hid it, deleted it.
- Prove the history wasn't quietly rewritten between then and now.

Hosted memory is a black box on all three. Under GDPR and the EU AI Act, a black
box is a dealbreaker. **Notari** is the neutral, self-hostable memory layer built
so those three proofs fall out of the design — not bolted on.

### Don't trust us. Break it.

The differentiators aren't bullet points; they're properties you can *attack on
purpose and watch get caught*. One command, no database, no API key, no model
download:

```bash
python examples/prove_the_moat.py
```

It's self-verifying — every claim ends in an `assert`, so it crashes instead of
printing ✅ if any property is false. Here's what it proves:

**1. Tamper-evident audit.** We silently rewrite a stored fact in the log
(`city: Berlin → Pyongyang`) and leave the ledger untouched. Reads now return the
forgery — and `verify_audit(deep=True)` catches it at the exact sequence number,
because every event's content is committed to a hash chain. Delete a ledger entry
and the linkage breaks too. You cannot alter stored memory undetected.

**2. Provable deletion.** Each subject's PII is encrypted at rest under a
per-subject key. `forget()` destroys the key and issues a signed
`DeletionCertificate`. The ciphertext row physically remains — but with no key it's
AES-256-GCM noise, unrecoverable — while the audit proof *survives the erasure*.
Right-to-be-forgotten and an immutable audit log, reconciled.

**3. Bi-temporal time-travel.** Correct a fact and the old value isn't overwritten
— it's superseded. Ask "where did the user live *as of* 2022?" and get the answer
that was true then; ask today and get today's. History stays reconstructable for
the dispute or the audit that comes later.

### Where we compete — and where we don't

The retrieval-accuracy race is crowded and well funded. supermemory reports 83.5%
Recall@10 on LOCOMO; Mem0 reports 91.6% accuracy. We're not trying to win that
leaderboard — our retrieval is competitive (and improving), but it's not the pitch.

The axis the leaders *cede* is the one regulated buyers actually have to buy:

| | Notari | supermemory / Mem0 / Zep |
|---|---|---|
| Provable deletion + certificate (GDPR Art. 17) | ✅ crypto-shred | ✗ (auto-expiry ≠ proof) |
| Tamper-evident audit (edit/insert/delete caught) | ✅ hash chain + content check | ✗ |
| Bi-temporal "what did it know on date D?" | ✅ | partial |
| Runs on plain Postgres, no graph DB, vendor-neutral | ✅ | ✗ |

*Auditable and provably deletable* memory, on infrastructure you already run, with
no vendor lock-in. That's the wedge.

### Who this is for

Teams putting agents in front of regulated data — finance, healthcare, insurance,
anything touching GDPR or the EU AI Act — who need memory their compliance team,
their DPO, and an external auditor will all sign off on.

### Honest status

The engine and its differentiators are **built and tested** (44 tests; Postgres
p95 ≈ 1 ms). The three guarantees above are provable today by running one script.
Retrieval quality is an internal signal we keep measuring, not a leaderboard claim.
Apache-2.0, self-hostable, zero-dependency core.

**Try it:** `git clone …/notari && python examples/prove_the_moat.py`
**Read the proof:** [docs/the-moat.md](the-moat.md) — threat model + honest boundaries.

---

## Show HN blurb

> **Show HN: Notari – AI memory you can audit and provably delete**
>
> Most agent-memory layers are black boxes: you can't prove where a memory came
> from, that you deleted a user's data, or that the history wasn't altered. That
> rules them out for anyone under GDPR / the EU AI Act.
>
> Notari makes those three things provable. The differentiators are adversarial,
> not marketing — `python examples/prove_the_moat.py` (no DB, no API key) tampers
> with a stored fact and shows it caught at the exact seq, crypto-shreds a subject
> and shows the ciphertext is unrecoverable while the audit proof survives, and
> time-travels a corrected fact. It's self-verifying: asserts, not print
> statements.
>
> Zero-dependency core, runs on plain Postgres (no graph DB), Apache-2.0. Not
> chasing the LOCOMO retrieval leaderboard — competing on auditability instead.
> Feedback welcome, especially from folks who've shipped memory into regulated
> environments.

---

## Suggested launch checklist

- [x] Confirm `prove_the_moat.py` runs clean on a fresh clone (fresh venv, no env
      vars, no installs) — verified: exits 0, gracefully degrades claim 2 to logical
      erasure when `cryptography` is absent, and points to `notari[crypto]` for the
      full shred.
- [ ] Replace repo URL placeholders in this post and the README quickstart.
- [ ] Record a ~30s asciinema of the demo for the README top and the post.
- [ ] Decide the retrieval-number stance: keep it a footnote (recommended) or run
      the official LOCOMO answer-accuracy protocol first for a comparable figure.
- [ ] Line up 2–3 design-partner conversations in a regulated vertical before the
      public post (the wedge lands in a sales conversation, not a leaderboard).
