"""Plain value records returned across the API.

Kept in their own module so both `memory` and the storage/backend adapters can
import them without a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DeletionCertificate:
    """Proof retained after a subject's data is destroyed (GDPR Art. 17).

    When encryption is enabled (`ATTESTARI_KEK`), `signature` is HMAC-SHA256 over
    the canonical certificate payload under a KEK-derived key (see
    `crypto.sign_certificate` / `crypto.verify_certificate`) — the certificate
    can't be forged or altered without the root key. Without a KEK the
    certificate is issued unsigned (`signature is None`)."""

    certificate_id: str
    subject_id: str
    requested_by: str
    episodes_deleted: int
    facts_deleted: int
    manifest_hash: str
    issued_at: datetime
    signature: str | None = None
    algorithm: str | None = None


@dataclass(frozen=True, slots=True)
class Provenance:
    fact_id: str
    source_episode_id: str
    recorded_at: datetime
    snippet: str | None
    source_ref: str | None
