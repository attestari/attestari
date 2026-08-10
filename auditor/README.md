# The auditor pack

For the people who have to **answer for** an AI system's memory — data
protection officers, compliance and risk teams, internal audit, and the
engineers who get handed their questions.

Attestari is an open-source memory layer for AI agents whose distinguishing
property is that its guarantees are **checkable by you**, not asserted by the
vendor. There is no Attestari company account to trust: the ledger sits in your
own database, and the verification commands run against it with read access
alone.

## What's in here

| Document | Use it for |
|---|---|
| [dpo-one-pager.md](dpo-one-pager.md) | The plain-language summary: what is guaranteed, what isn't, and how to check each claim yourself. Start here. |
| [eu-ai-act-article-12.md](eu-ai-act-article-12.md) | Mapping Article 12 (record-keeping) requirements to the specific mechanism that satisfies each, and the command that demonstrates it. |
| [gdpr-article-17.md](gdpr-article-17.md) | How erasure works here (cryptographic erasure), the honest regulatory position, and the deployment policies erasure depends on. |

## The evidence bundle

The pack is not only prose. Any deployment can produce a dated, re-derivable
snapshot of its own verifiable state:

```bash
attestari evidence --deep --out ./evidence
```

This writes `EVIDENCE.md` (a readable transcript) and `evidence.json`
(machine-readable) containing the audit-chain verification result and head
hash, an erasure register listing every erasure request re-checked against the
current ledger, and the deletion certificates the deployment has retained.

The bundle re-derives every claim from the live ledger at generation time — it
never trusts a previously exported artifact. That is the point: it is evidence
because **you can regenerate it**, not because someone handed it to you.

## A note on what this pack is not

These documents describe how a piece of software works and which technical
measures it provides. They are **not legal advice**, and they are not a
certification. Whether a given deployment satisfies a given obligation depends
on facts this repository cannot see: your data, your retention policy, your key
management, your processing purposes. Take these documents to your own counsel
or DPA as a technical description — that is what they are written to be.

Where the regulatory position is unsettled, these documents say so rather than
resolving it in Attestari's favour. If you find a claim here that overstates
what the software does, that is a bug — please open an issue.
