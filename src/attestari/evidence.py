"""Evidence bundle: a regulator-facing snapshot of the ledger's verifiable state.

`build_evidence()` re-derives everything from the live ledger at generation
time — it does not trust any previously exported artifact. The bundle contains
the audit-chain verification result (head hash included), an erasure register
(every `subject_forgotten` on record, each re-checked against the current
projection), and the signed deletion certificates where the storage tier
persists them. `render_markdown()` turns the same bundle into a transcript a
DPO or auditor can read — including the commands to reproduce every claim
independently, which is the point: this is evidence because it can be
re-derived, not because we exported it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import __version__
from .events import SubjectForgotten


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def build_evidence(memory, *, deep: bool = False) -> dict[str, Any]:
    """Assemble the bundle as a JSON-serialisable dict.

    `ok` is the auditor's one-glance answer: the chain holds *and* every
    recorded erasure is complete (no live facts remain for a forgotten
    subject). Anything less is reported, not smoothed over.
    """
    report = memory.verify_audit(deep=deep)

    # The erasure register: every forget on record, re-checked now. A repeated
    # forget of the same subject collapses to one row (latest request wins).
    forgets: dict[str, SubjectForgotten] = {}
    for event in memory.store.events():
        if isinstance(event, SubjectForgotten):
            forgets[event.subject_id] = event
    erasures = []
    for event in forgets.values():
        live = memory.timeline(subject_id=event.subject_id)
        erasures.append(
            {
                "subject_id": event.subject_id,
                "requested_by": event.requested_by,
                "recorded_at": _iso(event.recorded_at),
                "erased": not live,
                "live_facts": len(live),
            }
        )
    erasures.sort(key=lambda row: row["recorded_at"])

    # Signed certificates, where the tier persists them (Postgres). SQLite and
    # in-memory tiers hand the certificate to the caller at forget() time and
    # keep none — `certificates: null` reports that honestly rather than
    # implying an empty history.
    certificates: list[dict[str, Any]] | None = None
    read_certs = getattr(memory.backend, "certificates", None)
    if callable(read_certs):
        certificates = []
        for cert in read_certs():
            certificates.append(
                {
                    "certificate_id": cert.certificate_id,
                    "subject_id": cert.subject_id,
                    "requested_by": cert.requested_by,
                    "episodes_deleted": cert.episodes_deleted,
                    "facts_deleted": cert.facts_deleted,
                    "manifest_hash": cert.manifest_hash,
                    "issued_at": _iso(cert.issued_at),
                    "signature": cert.signature,
                    "algorithm": cert.algorithm,
                }
            )

    return {
        "attestari_version": __version__,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "store": type(memory.store).__name__,
        "verification": {
            "mode": "deep" if deep else "chain",
            "ok": report.ok,
            "entries": report.entries,
            "head": report.head,
            "broken_at": report.broken_at,
        },
        "erasures": erasures,
        "certificates": certificates,
        "ok": report.ok and all(row["erased"] for row in erasures),
    }


def render_markdown(bundle: dict[str, Any]) -> str:
    """The human transcript of the same bundle, reproduction commands included."""
    v = bundle["verification"]
    chain_result = "intact" if v["ok"] else f"BROKEN at seq {v['broken_at']}"
    lines = [
        "# Attestari evidence bundle",
        "",
        f"Generated {bundle['generated_at']} by attestari {bundle['attestari_version']} "
        f"({bundle['store']}). Overall: **{'PASS' if bundle['ok'] else 'FAIL'}**.",
        "",
        "Every claim below was re-derived from the live ledger when this bundle was",
        "generated, and can be re-derived again by anyone with read access — see",
        "*Reproduce this yourself* at the end. Machine-readable copy: `evidence.json`.",
        "",
        "## 1. Audit-chain verification",
        "",
        f"- Mode: **{v['mode']}**"
        + ("" if v["mode"] == "deep" else " (link check; rerun with `--deep` to re-hash content)"),
        f"- Result: **{chain_result}**",
        f"- Entries: {v['entries']}",
        f"- Head hash: `{v['head']}`",
        "",
        "The head hash commits to the entire event history; record it externally",
        "(ticket, email, timestamping service) and any later rewrite of history is",
        "detectable by comparing heads.",
        "",
        "## 2. Erasure register",
        "",
    ]
    if bundle["erasures"]:
        lines += [
            "| Subject | Requested by | Recorded (UTC) | Status |",
            "|---|---|---|---|",
        ]
        for row in bundle["erasures"]:
            status = (
                "erased — no live data"
                if row["erased"]
                else f"**INCOMPLETE — {row['live_facts']} live fact(s)**"
            )
            lines.append(
                f"| `{row['subject_id']}` | {row['requested_by']} | {row['recorded_at']} | {status} |"
            )
    else:
        lines.append("No erasure requests on record.")
    lines += ["", "## 3. Deletion certificates", ""]
    if bundle["certificates"] is None:
        lines.append(
            "This storage tier does not persist certificates (they are returned to the "
            "caller at `forget()` time). The erasure register above is re-verified "
            "against the current ledger, which is the stronger claim."
        )
    elif not bundle["certificates"]:
        lines.append("No certificates persisted.")
    else:
        lines += [
            "| Certificate | Subject | Episodes | Facts | Issued (UTC) | Signed |",
            "|---|---|---|---|---|---|",
        ]
        for cert in bundle["certificates"]:
            signed = cert["algorithm"] if cert["signature"] else "unsigned"
            lines.append(
                f"| `{cert['certificate_id'][:8]}…` | `{cert['subject_id']}` "
                f"| {cert['episodes_deleted']} | {cert['facts_deleted']} "
                f"| {cert['issued_at']} | {signed} |"
            )
    lines += [
        "",
        "## Reproduce this yourself",
        "",
        "```bash",
        "pip install attestari",
        "attestari verify --deep          # re-check the chain, re-hash content",
        "attestari verify --user <id>     # re-check one subject's erasure",
        "attestari evidence --deep        # regenerate this bundle",
        "```",
        "",
        "Point `ATTESTARI_DATABASE_URL` at the deployment's Postgres (or run beside",
        "its SQLite file) — verification needs only read access and no cooperation",
        "from the operator. That independence is the guarantee.",
        "",
    ]
    return "\n".join(lines)
