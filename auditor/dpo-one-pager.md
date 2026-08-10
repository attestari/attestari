# Attestari for data protection officers — the one-pager

**What it is.** A memory layer for AI agents: it stores what an agent learns
about people across sessions, and retrieves it later. What makes it different
from other memory layers is that its records are **tamper-evident** and its
deletions are **cryptographic and provable**.

**Why that matters to you.** AI systems that remember create two obligations
that pull against each other: keep records of what the system did (EU AI Act
Art. 12), and erase a person's data when they ask (GDPR Art. 17). Systems that
resolve this by "soft deleting" — hiding a row while keeping the data — satisfy
neither cleanly. Attestari is built so that the content can be destroyed while
the *proof of what happened* survives.

---

## The three guarantees, in plain language

**1. Nobody can alter stored memory without it being detectable.**
Every event is chained to the one before it by a cryptographic hash. Changing
any past record — a fact, its source, its timestamp — breaks the chain at that
exact point, and verification reports the position of the break. This includes
alteration by the system operator.

**2. When a person is erased, their content becomes unrecoverable.**
Each person's data is encrypted under a key that belongs only to them. Erasure
destroys that key. What remains is AES-256-GCM ciphertext, which without the key
is indistinguishable from random noise — including in any backup that already
contains it. This is *cryptographic erasure* (crypto-shredding).

**3. The erasure leaves a proof, and the proof survives.**
Erasure issues a signed **deletion certificate** recording the subject, who
requested it, how many records were destroyed, a manifest hash, and the time.
The audit chain still verifies afterwards — the record that the deletion
happened is not destroyed along with the data.

---

## How you check each claim yourself

You do not have to take the above on trust, and you do not need the vendor's
cooperation — there is no vendor in the loop. With read access to the
deployment's database:

```bash
pip install attestari

attestari verify --deep        # re-check the whole chain, re-hashing content
attestari verify --user u_123  # confirm one person's data is provably erased
attestari evidence --deep      # produce a dated evidence bundle
```

`verify` exits non-zero if anything fails, so it can run as a scheduled control
rather than a manual exercise. `evidence` writes `EVIDENCE.md` and
`evidence.json` — see [README.md](README.md).

**The head hash is your anchor.** Verification prints a single hash committing
to the entire history. Record it somewhere outside the system (a ticket, an
email to yourself, a timestamping service). If the history is ever rewritten,
the head hash at your next check will not match the one you recorded.

---

## What is *not* guaranteed — read this part

A control you misunderstand is worse than one you don't have.

- **Cryptographic erasure is not physical overwriting.** The guarantee is "the
  key is destroyed and AES-256-GCM is sound," not "the bytes were overwritten on
  every replica." If your policy requires physical destruction of media, this
  mechanism does not provide it.

- **Erasure depends on key-storage policy.** A database backup taken *before* an
  erasure also contains that person's wrapped key. Restoring it while the root
  key still exists would bring the data back. Deployments must therefore do one
  of: exclude the `keyring` table from ordinary backups, rotate the root key on a
  schedule and destroy old versions, or hold the root key in a KMS with enforced
  version destruction. **This is a deployment policy question, not something the
  software can decide for you** — ask the engineering team which one is in place.

- **Search indexes hold readable text.** To make memory searchable, derived
  tables (`edge`, `entity`) hold fact text in the clear. They are rebuilt on
  every erasure so the live system is clean, but a backup taken beforehand would
  contain them. These tables are fully rebuildable from the log, so the policy is
  to exclude them from backups entirely. Same bucket as the keyring.

- **Without encryption configured, erasure is logical, not cryptographic.** If
  no root key (`ATTESTARI_KEK`) is set, `forget()` drops the content from all
  reads and still issues a certificate, but nothing is cryptographically
  shredded — and the certificate is deliberately left **unsigned**, because
  there is no root key to anchor a signature to. An unsigned certificate is a
  signal, not an oversight: it means this deployment is in logical-delete mode.
  Confirm which mode you are in before relying on the erasure claim.

- **The chain proves internal consistency, not external truth.** It proves the
  history has not been altered since it was written. It cannot prove that what
  was written was accurate, nor stop an operator who controls the system from
  discarding the entire ledger and starting a new one. Anchoring the head hash
  externally (above) is what closes that gap, and it is a manual step today.

- **This is not legal advice or a certification.** It is a technical
  description. Whether your deployment meets a given obligation depends on your
  data, retention policy, and key management.

---

## Questions worth asking your engineering team

1. Is `ATTESTARI_KEK` set in production — i.e. are we in cryptographic-erasure
   mode or logical-delete mode?
2. Where does the root key live, and who can access it?
3. Which key-backup policy did we choose (exclude / rotate / KMS destruction)?
4. Are `keyring`, `edge`, and `entity` excluded from database backups?
5. Is `attestari verify --deep` running on a schedule, and who sees a failure?
6. Where do we record the head hash externally, and how often?
