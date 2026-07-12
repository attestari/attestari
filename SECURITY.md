# Security Policy

Attestari is security-sensitive infrastructure (encryption, provable deletion, a
tamper-evident audit chain), so we take reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Instead, use **GitHub's private vulnerability reporting** ("Report a
vulnerability" under the repo's Security tab), or email **attestari.hq@gmail.com**.
We aim to acknowledge within 72 hours and to coordinate a fix and disclosure
timeline with you.

Useful things to include: affected version/commit, a reproduction, and the
impact (e.g., "deletion is recoverable," "audit chain can be forged without
detection," "PII leaks across subjects").

## Areas of particular interest

- **Crypto-shred deletion** (`crypto.py`) — any way to recover a subject's data
  after `forget()` destroys the key.
- **Audit chain** (`audit.py`) — any way to alter history without
  `verify_audit()` detecting it.
- **Subject isolation** — any cross-subject data leak in retrieval or projection.
- **Key handling** — the KEK is read from `ATTESTARI_KEK`; production should use a
  KMS (planned). Reports about key exposure are welcome.

## Scope notes

- The default `HashEmbedder` and `DeterministicExtractor` are stand-ins, not
  security boundaries.
- `subject_id` is a plaintext partition key — it must be an opaque pseudonym, not
  raw PII. Misuse here is a deployment concern, but we're happy to discuss
  hardening.
- Crypto-shred makes every backed-up copy of the *content* unrecoverable, but a
  backup of the `keyring` table taken before a shred can restore the wrapped
  key. Deployments must exclude the keyring from ordinary backups, rotate the
  KEK, or hold it in a KMS — see the threat model in
  [docs/the-moat.md](docs/the-moat.md). This is inherent to crypto-shred, not a
  vulnerability, but reports on the key lifecycle are very welcome.
