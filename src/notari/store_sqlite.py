"""SQLiteEventStore — the durable, zero-infrastructure adapter.

The middle rung of the storage ladder: `Memory()` (in-memory, ephemeral) →
**`Memory.local()` (this: one SQLite file, survives restarts, stdlib only)** →
`Memory.postgres()` (concurrent production service with SQL hybrid retrieval).
Built for the one-process case — a personal agent, an MCP server spawned by
Claude Desktop, a prototype that must not lose memory on restart.

Implements the same `EventStore` protocol as the other adapters and inherits
the guarantees from the same shared components — `audit.next_entry` extends the
chain, `crypto.KeyManager` owns the DEK lifecycle — so the moat properties
(tamper-evident history, crypto-shred deletion, deep verification) hold here by
construction, not reimplementation.

Storage model: each event is one row, serialized to JSON (PII fields already
encrypted when a cipher is on), with `seq` = its audit entry's seq — one total
append order, so deep verification aligns entries to events exactly. Appends
are atomic (event row + audit row in one `BEGIN IMMEDIATE` transaction) and
serialized across threads and processes, so the chain can never fork. WAL
journaling with `synchronous=FULL` makes a committed append survive a crash.

Concurrency contract: safe across threads in one process and across processes
on one machine (file locking); it is **not** a multi-writer server — that's
what `Memory.postgres()` is for.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .audit import GENESIS, AuditEntry, next_entry
from .crypto import EnvelopeCipher, KeyManager, NullCipher, cipher_from_env
from .events import (
    EntityMerged,
    EntityUnmerged,
    EpisodeIngested,
    Event,
    FactAsserted,
    FactInvalidated,
    Scope,
    SubjectForgotten,
)

DEFAULT_PATH = "~/.notari/notari.db"

_EVENT_TYPES: dict[str, type] = {
    "episode_ingested": EpisodeIngested,
    "fact_asserted": FactAsserted,
    "fact_invalidated": FactInvalidated,
    "entity_merged": EntityMerged,
    "entity_unmerged": EntityUnmerged,
    "subject_forgotten": SubjectForgotten,
}

_DT_FIELDS = ("ingested_at", "valid_from", "valid_to", "recorded_at")


def _encode(event: Event) -> str:
    """Event -> JSON. Datetimes as isoformat (µs + offset preserved, so audit
    digests re-derive byte-identically after a round-trip); everything else is
    JSON-native. `dataclasses.asdict` folds the nested Scope to a dict."""
    d = dataclasses.asdict(event)
    for k in _DT_FIELDS:
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return json.dumps(d, ensure_ascii=False)


def _decode(kind: str, payload: str) -> Event:
    """JSON -> the exact original event (tuple spans, Scope, aware/naive
    datetimes all restored faithfully)."""
    cls = _EVENT_TYPES[kind]
    d = json.loads(payload)
    for k in _DT_FIELDS:
        if d.get(k) is not None:
            d[k] = datetime.fromisoformat(d[k])
    if d.get("char_span") is not None:
        d["char_span"] = tuple(d["char_span"])
    if d.get("scope") is not None:
        d["scope"] = Scope(**d["scope"])
    return cls(**d)


class _SQLiteKeyring:
    """Keyring adapter over the `keyring` table (wrapped DEKs at rest)."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def get(self, subject_id: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT wrapped_dek FROM keyring WHERE subject_id = ?", (subject_id,)
            ).fetchone()
        return bytes(row["wrapped_dek"]) if row is not None else None

    def put(self, subject_id: str, wrapped_dek: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO keyring (subject_id, wrapped_dek) VALUES (?, ?)",
                (subject_id, wrapped_dek),
            )

    def delete(self, subject_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM keyring WHERE subject_id = ?", (subject_id,))

    def all(self) -> dict[str, bytes]:
        with self._lock:
            rows = self._conn.execute("SELECT subject_id, wrapped_dek FROM keyring").fetchall()
        return {r["subject_id"]: bytes(r["wrapped_dek"]) for r in rows}


class SQLiteEventStore:
    """Durable, totally-ordered event log in a single local SQLite file."""

    def __init__(
        self,
        path: str | os.PathLike | None = None,
        cipher: NullCipher | EnvelopeCipher | None = None,
    ) -> None:
        p = Path(path if path is not None else DEFAULT_PATH).expanduser()
        if str(p) != ":memory:":
            existed = p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            self._restrict(p.parent, 0o700)
            if not existed:
                p.touch()
            # The file can hold plaintext (NullCipher) or wrapped DEKs — either
            # way it's private data: owner-only, best effort (no-op on Windows).
            self._restrict(p, 0o600)
        self.path = p
        # isolation_level=None -> autocommit; transactions are explicit (BEGIN
        # IMMEDIATE), so an append is exactly one atomic unit. The RLock
        # serialises threads in this process; BEGIN IMMEDIATE + busy_timeout
        # serialise other processes on the same file.
        self._conn = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")  # a committed append survives a crash
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS event (
                seq     INTEGER PRIMARY KEY,  -- = audit_entry.seq (global append order)
                kind    TEXT NOT NULL,
                payload TEXT NOT NULL         -- JSON event (PII already encrypted at rest)
            );
            CREATE TABLE IF NOT EXISTS audit_entry (
                seq          INTEGER PRIMARY KEY,
                kind         TEXT NOT NULL,
                ref          TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                prev_hash    TEXT NOT NULL,
                entry_hash   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS keyring (
                subject_id  TEXT PRIMARY KEY,
                wrapped_dek BLOB NOT NULL
            );
            """
        )
        # Encryption is opt-in: EnvelopeCipher when NOTARI_KEK is set, else NullCipher.
        self.cipher = cipher or cipher_from_env()
        self._keys = KeyManager(self.cipher, _SQLiteKeyring(self._conn, self._lock))

    @staticmethod
    def _restrict(p: Path, mode: int) -> None:
        try:
            p.chmod(mode)
        except OSError:  # pragma: no cover - platform-dependent (e.g. some Windows FS)
            pass

    # --- crypto-shred key management ------------------------------------ #

    def shred_subject(self, subject_id: str) -> None:
        """Destroy the subject's DEK — their ciphertext becomes unrecoverable."""
        self._keys.shred(subject_id)

    def erased_refs(self) -> set[str]:
        """Ids of episodes/facts whose content was **sanctioned-erased**: the
        subject's DEK is destroyed AND a `SubjectForgotten` tombstone is on the
        log. Deep audit verification skips exactly these entries; a destroyed
        key with no tombstone is deliberately NOT included, so a rogue key
        deletion fails verification instead of hiding."""
        if not self.cipher.enabled:
            return set()
        live = set(self._keys.keyring.all())
        out: set[str] = set()
        tombstoned: set[str] = set()
        raw = self._raw_events()
        for ev in raw:
            if isinstance(ev, SubjectForgotten):
                tombstoned.add(ev.subject_id)
        gone = {sid for sid in tombstoned if sid not in live}
        for ev in raw:
            if isinstance(ev, EpisodeIngested) and ev.scope.subject_id in gone:
                out.add(ev.episode_id)
            elif isinstance(ev, FactAsserted) and ev.scope.subject_id in gone:
                out.add(ev.fact_id)
        return out

    # --- write path ----------------------------------------------------- #

    def append(self, event: Event) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Encrypt inside the transaction so a freshly-minted DEK commits
                # atomically with the event it protects.
                stored = event
                if self.cipher.enabled:
                    if isinstance(event, EpisodeIngested) and event.scope.subject_id:
                        stored = dataclasses.replace(
                            event,
                            payload=self._keys.encrypt_for(event.scope.subject_id, event.payload),
                        )
                    elif isinstance(event, FactAsserted) and event.scope.subject_id:
                        stored = dataclasses.replace(
                            event,
                            object=self._keys.encrypt_for(event.scope.subject_id, event.object),
                        )

                row = self._conn.execute(
                    "SELECT seq, entry_hash FROM audit_entry ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                prev = row["entry_hash"] if row else GENESIS
                # Chain extension from the shared helper (digest of the
                # *plaintext* event); seq stamps the event row too, so events()
                # reads back in exactly the chained order.
                entry = next_entry(prev, (row["seq"] + 1) if row else 1, event)
                self._conn.execute(
                    "INSERT INTO event (seq, kind, payload) VALUES (?, ?, ?)",
                    (entry.seq, event.op, _encode(stored)),
                )
                self._conn.execute(
                    """INSERT INTO audit_entry
                           (seq, kind, ref, payload_hash, prev_hash, entry_hash)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (entry.seq, entry.kind, entry.ref, entry.payload_hash,
                     entry.prev_hash, entry.entry_hash),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # --- read path ------------------------------------------------------ #

    def _raw_events(self) -> list[Event]:
        """All stored events in append order, WITHOUT decryption or skipping
        (PII fields may be ciphertext). Internal: erased_refs scans this."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, payload FROM event ORDER BY seq"
            ).fetchall()
        return [_decode(r["kind"], r["payload"]) for r in rows]

    def events(self) -> list[Event]:
        raw = self._raw_events()
        if not self.cipher.enabled:
            return raw

        deks = self._keys.live_deks()
        out: list[Event] = []
        for ev in raw:
            if isinstance(ev, EpisodeIngested) and ev.scope.subject_id:
                sid = ev.scope.subject_id
                if sid not in deks:
                    continue  # DEK destroyed -> subject erased: episode is unreadable
                out.append(
                    dataclasses.replace(ev, payload=self.cipher.decrypt(deks[sid], ev.payload))
                )
            elif isinstance(ev, FactAsserted) and ev.scope.subject_id:
                sid = ev.scope.subject_id
                if sid not in deks:
                    continue  # source subject erased -> the fact is erased too
                out.append(
                    dataclasses.replace(ev, object=self.cipher.decrypt(deks[sid], ev.object))
                )
            else:
                out.append(ev)
        return out

    def audit_entries(self) -> list[AuditEntry]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit_entry ORDER BY seq").fetchall()
        return [
            AuditEntry(
                seq=r["seq"],
                kind=r["kind"],
                ref=r["ref"],
                payload_hash=r["payload_hash"],
                prev_hash=r["prev_hash"],
                entry_hash=r["entry_hash"],
            )
            for r in rows
        ]

    # --- helpers -------------------------------------------------------- #

    def close(self) -> None:
        self._conn.close()

    def __len__(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT count(*) AS n FROM event").fetchone()["n"]
