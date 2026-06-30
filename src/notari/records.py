"""Plain value records returned across the API.

Kept in their own module so both `memory` and the storage/backend adapters can
import them without a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DeletionCertificate:
    """Proof retained after a subject's data is destroyed (GDPR Art. 17)."""

    certificate_id: str
    subject_id: str
    requested_by: str
    episodes_deleted: int
    facts_deleted: int
    manifest_hash: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class Provenance:
    fact_id: str
    source_episode_id: str
    recorded_at: datetime
    snippet: str | None
    source_ref: str | None
