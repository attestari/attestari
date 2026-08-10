# GDPR Article 17 (right to erasure) — how erasure works here

**Not legal advice.** This describes a technical measure and the state of the
regulatory discussion around it, so that you and your counsel can assess it. It
does not tell you whether your deployment satisfies Art. 17.

## The problem this addresses

An AI system that keeps an append-only record of what it learned, and a data
subject who asks to be erased, look like a contradiction: the record must be
retained, the person's data must go. The usual resolutions are unsatisfying —
soft-delete a row (the data is still there), or scrub every replica and backup
(expensive, error-prone, and often incomplete in ways nobody can prove).

## What Attestari actually does

`forget(subject_id)` performs **cryptographic erasure** (crypto-shredding):

1. Every subject's content is encrypted at rest under a per-subject data key
   (DEK), itself wrapped under the deployment's root key (KEK).
2. `forget()` destroys the wrapped DEK.
3. What remains is AES-256-GCM ciphertext. Without the DEK it is
   computationally infeasible to recover — including in every backup and replica
   that already holds that ciphertext, which is the property that makes this
   tractable at all.
4. A `subject_forgotten` event is appended, and a **deletion certificate** is
   issued: subject, requester, counts, manifest hash, timestamp — HMAC-SHA256
   signed under a key derived from the KEK (domain-separated from content
   encryption). `attestari.crypto.verify_certificate` recomputes it, so a forged
   certificate, or a genuine one with any field altered, fails verification.
5. The audit chain still verifies, because it commits to content *digests*
   rather than content. The proof of the erasure survives the erasure.

Verify an individual erasure at any time:

```bash
attestari verify --user u_123     # non-zero exit if the claim doesn't hold
```

This checks the ledger as it stands *now*: that a forget is on record for that
subject and that no live data remains for them. It is a re-derivation, not a
lookup of something we wrote down earlier.

## The honest regulatory position

This is the part most vendor documents overstate, so to be precise:

- **The EDPB has not formally endorsed crypto-shredding as Article 17 erasure.**
  Anyone telling you regulators have blessed it is going further than the record
  supports.
- EDPB guidance does require that erasure be **irreversible** — and the argument
  for cryptographic erasure is that destroying the only key makes recovery
  infeasible, which meets that bar in substance.
- Several national data protection authorities have **accepted** cryptographic
  erasure in practice, particularly where physically scrubbing every copy would
  be disproportionate effort. Acceptance in specific cases is not the same as
  general endorsement.
- **Erasure from backups is an area of active regulatory attention** — DPAs have
  asked the EDPB for guidance on it. Expect this position to develop.

The practical consequence: cryptographic erasure is a **defensible technical
measure with a real argument behind it and genuine open questions**, not a
settled safe harbour. Attestari's contribution is that its erasure is
*evidenced* — you can demonstrate what was destroyed and when, and a third party
can re-check it — which is the part you would otherwise have to assert on trust.
Take that to your DPA as a technical description and let them assess it.

## What erasure depends on — the deployment policies

Cryptographic erasure is only as good as the key handling around it. Three
exceptions need an explicit policy; none of them can be decided by the library.

**1. The key store.** A backup taken before the shred contains the subject's
wrapped DEK. Restoring it while the KEK still lives resurrects the data. Choose
one: exclude the `keyring` table from ordinary backups (the standard
crypto-shred deployment pattern, with separate short-retention key backups);
rotate the KEK on a schedule and destroy old versions (bounding resurrection to
the rotation window); or hold the KEK in a KMS with enforced key-version
destruction.

**2. The projection tables.** Retrieval needs plaintext — you cannot full-text
search ciphertext — so the derived `edge` and `entity` tables hold fact text and
embeddings in the clear. They are dropped and rebuilt on every `forget()`, so
the live database is clean, but a backup taken beforehand would contain them.
They are fully rebuildable from the log, so exclude them from backups entirely.

**3. Encryption must actually be on.** With no KEK configured, `forget()` is a
logical delete: content is dropped from all reads and a certificate is still
issued, but nothing is cryptographically shredded, and the certificate is left
**unsigned** by design. Verify which mode production is in before relying on the
erasure claim.

These are properties of every crypto-shred system that also has to be
searchable, not Attestari quirks. They are documented here rather than left for
an audit to discover.

## Scope of an erasure

`forget(subject_id)` erases the content associated with that subject in the
memory layer: their episodes and the facts derived from them. It does not reach
data your application holds elsewhere, model weights that may have been trained
on the data, or copies exported to other systems. Memory is one component of a
data map, and Art. 17 applies to all of it.

## Previewing before you erase

Erasure is one-way. `forget(dry_run=True)` computes and returns the certificate
that *would* be issued — same subject, counts, and manifest hash — while
destroying nothing, so the blast radius can be confirmed first. The preview is
deliberately left unsigned, so a dry run can never be mistaken for, or verify
as, a real deletion proof.
