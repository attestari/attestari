# EU AI Act Article 12 (record-keeping) — what Attestari provides

**Scope note.** Article 12 applies to **high-risk** AI systems, and its
obligations fall on the *provider* of that system — not on a library it uses.
Attestari is a component. It cannot make a system compliant; it can supply and
evidence the record-keeping capability that Art. 12 requires a high-risk system
to have. Obligations became enforceable **2 August 2026**. This document is a
technical mapping, **not legal advice**.

## The requirement

Article 12 requires that high-risk AI systems "technically allow for the
automatic recording of events (logs) over the lifetime of the system," with
logging capabilities appropriate to the intended purpose — enabling
identification of risk situations and substantial modifications, post-market
monitoring (Art. 72), and operational monitoring by deployers (Art. 26(5)).
Deployers must retain those logs for a period appropriate to the intended
purpose, and **at least six months** unless other law says otherwise.

Two properties follow that plain file logging tends not to satisfy: logs must be
generated **automatically** by the system (documentation written by hand does
not count), and they must be trustworthy enough to support post-market
investigation — which is why tamper-evidence is the common regulatory reading,
even though Art. 12 does not use that word.

## The mapping

| What Art. 12 requires | Mechanism in Attestari | How to demonstrate it |
|---|---|---|
| Automatic recording of events | Every write is an event appended to the log by the engine itself — `episode_ingested`, `fact_asserted`, `fact_invalidated`, `entity_merged`, `subject_forgotten`. There is no code path that records memory without an event. | `attestari evidence --deep` reports the entry count; the event log *is* the store, not a side-channel. |
| Over the lifetime of the system | The log is append-only and event-sourced: history is never mutated in place, and current state is a projection rebuilt from it. Retention is bounded only by your storage policy. | `attestari verify --deep` walks the entire chain from genesis and reports the total entries. |
| Records trustworthy enough for post-market investigation | Hash-linked chain: each entry commits to the previous entry and to a digest of its own payload. Any alteration of a past record breaks the chain at that point. | `attestari verify --deep` re-hashes stored content and reports `broken_at` — the exact sequence number of the first inconsistency. |
| Traceability of *why* the system knew something | Every fact carries provenance: source episode, character span, and both time axes (when it was true, when it was recorded). | `get_provenance(fact_id)` returns the source and span; `timeline()` returns the bi-temporal history. |
| Reconstructing what the system believed at a past moment | Bi-temporal `as_of` queries reconstruct the state of knowledge at any past timestamp — without deleting the corrections that came after. | `search(..., as_of="2026-03-01")` answers "what did it believe then." |
| Records that survive an erasure request | The chain hashes content *digests*, not content. Destroying a subject's content leaves the chain intact and verifiable. | Erase a subject, then `attestari verify --deep` — still `ok`. See [gdpr-article-17.md](gdpr-article-17.md). |
| Evidence a deployer can hand to an authority | The evidence bundle: verification result, head hash, erasure register, retained certificates — each item re-derived from the live ledger. | `attestari evidence --deep --out ./evidence` |

## The six-month retention point

Retention is a deployment decision, not a library setting: the log persists in
your Postgres or SQLite until you remove it. Two things are worth stating
plainly to whoever sets that policy:

- Attestari does **not** expire or roll off records on its own. If you need a
  retention ceiling, you implement it.
- Erasing a subject under Art. 17 does not delete their *log entries* — it
  destroys their content while the entries, and the certificate recording the
  erasure, remain. This is the specific design that lets the "keep records" and
  "erase the person" obligations hold at once, rather than trading one off
  against the other.

## What this does not do

- It does not classify your system as high-risk or not, and does not produce the
  Art. 11 / Annex IV technical documentation.
- It logs **the memory layer's** events. Events elsewhere in your system —
  inference calls, human oversight actions, model changes — are outside its
  scope, and Art. 12 concerns the whole high-risk system.
- Tamper-*evidence* is not tamper-*proofing*: it makes alteration detectable,
  not impossible. An operator with full control can still discard the entire
  ledger. Recording the head hash externally on a schedule is what makes that
  detectable too, and it is a manual step today.
