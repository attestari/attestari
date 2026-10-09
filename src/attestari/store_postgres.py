"""PostgresEventStore — the durable event-log adapter.

Implements the same `EventStore` protocol as `InMemoryEventStore`, persisting
events to `src/attestari/db/schema.sql` (the `episode` and `fact_event` tables) and
reconstructing them on read. Because `Memory` and `Projector` consume only
`events()`, swapping this store in makes the whole engine durable — survives
process restarts — without changing a line of the core logic.

A fact's scope (subject_id, etc.) is recovered from its source episode rather
than duplicated on every fact row: provenance already ties each fact to the
episode it came from, and the episode owns the scope.

Requires the `postgres` extra:  pip install "attestari[postgres]"
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from .audit import GENESIS, AuditEntry, next_entry
from .crypto import EnvelopeCipher, KeyManager, NullCipher, cipher_from_env
from .embed import Embedder
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
from .backend import ProjectionCache
from .projection import Edge, Entity, Projection, Projector
from .records import DeletionCertificate
from .retrieve import SearchResult, weights_for
from .store import LOG_START, ForgottenSubjectError, LogPosition, content_subject

_DEFAULT_DSN = "postgresql://attestari:attestari@localhost:5432/attestari"

# Transaction-scoped advisory lock over the whole event log, taken by write_lock.
# Appends hold it so two writers can't fork the audit chain; projection updates
# hold it so they serialise with appends and with each other, across processes;
# Memory.add() holds it while it decides supersession and writes the result.
_LOG_LOCK = "SELECT pg_advisory_xact_lock(hashtext('attestari.event_log'))"


def _span(lo: int | None, hi: int | None) -> tuple[int, int] | None:
    return (lo, hi) if lo is not None and hi is not None else None


def _vec_literal(vec: list[float]) -> str:
    """pgvector text literal, e.g. '[0.1,0.2,...]' (cast to ::vector in SQL)."""
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


class _PostgresKeyring:
    """Keyring adapter over the `keyring` table (wrapped DEKs at rest)."""

    def __init__(self, conn, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def get(self, subject_id: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT wrapped_dek FROM keyring WHERE subject_id = %s", (subject_id,)
            ).fetchone()
        return bytes(row["wrapped_dek"]) if row is not None else None

    def put(self, subject_id: str, wrapped_dek: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO keyring (subject_id, wrapped_dek) VALUES (%s, %s)",
                (subject_id, wrapped_dek),
            )

    def delete(self, subject_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM keyring WHERE subject_id = %s", (subject_id,))

    def all(self) -> dict[str, bytes]:
        with self._lock:
            rows = self._conn.execute("SELECT subject_id, wrapped_dek FROM keyring").fetchall()
        return {r["subject_id"]: bytes(r["wrapped_dek"]) for r in rows}


class PostgresEventStore:
    """Durable, totally-ordered event log backed by Postgres."""

    def __init__(
        self, dsn: str | None = None, cipher: NullCipher | EnvelopeCipher | None = None
    ) -> None:
        import psycopg  # imported lazily so the core stays dependency-free
        from psycopg.rows import dict_row

        self.dsn = dsn or os.environ.get("ATTESTARI_DATABASE_URL", _DEFAULT_DSN)
        self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        # One connection serves every thread that shares this store (FastAPI
        # runs sync handlers on a thread pool). psycopg does not isolate
        # transaction blocks between threads — another thread's statement would
        # run inside ours — so every use of the connection holds this lock,
        # and the projection backend shares it.
        self._lock = threading.RLock()
        self._write_depth = 0  # see write_lock
        self._schema_current = False  # see _require_current_schema
        # Encryption is opt-in: EnvelopeCipher when ATTESTARI_KEK is set, else NullCipher.
        self.cipher = cipher or cipher_from_env()
        # Key lifecycle is delegated to the shared KeyManager; only the resting
        # place of the wrapped DEKs (the keyring table) is adapter-specific.
        self._keys = KeyManager(self.cipher, _PostgresKeyring(self._conn, self._lock))

    # --- crypto-shred key management ------------------------------------ #

    def shred_subject(self, subject_id: str) -> None:
        """Destroy the subject's DEK — their ciphertext becomes unrecoverable."""
        self._keys.shred(subject_id)

    def commit_keys(self) -> dict[str, bytes]:
        """Commitment keys of the subjects whose DEK is intact (deep verify)."""
        return self._keys.commit_keys()

    def erased_refs(self) -> set[str]:
        """Ids of episodes/facts whose content was **sanctioned-erased**: the
        subject's DEK is destroyed AND a `SubjectForgotten` tombstone is on the
        log. Deep audit verification skips exactly these entries; a destroyed
        key with no tombstone is deliberately NOT included, so a rogue key
        deletion fails verification instead of hiding."""
        if not self.cipher.enabled:
            return set()
        with self._lock:
            rows = self._conn.execute(
                """WITH gone AS (
                       SELECT DISTINCT f.subject AS sid FROM fact_event f
                       WHERE f.op = 'subject_forgotten'
                         AND NOT EXISTS (SELECT 1 FROM keyring k WHERE k.subject_id = f.subject)
                   )
                   SELECT e.episode_id::text AS ref
                   FROM episode e JOIN gone g ON e.subject_id = g.sid
                   UNION
                   SELECT f.fact_id::text AS ref
                   FROM fact_event f
                       JOIN episode e ON f.source_episode = e.episode_id
                       JOIN gone g ON e.subject_id = g.sid
                   WHERE f.op = 'asserted'"""
            ).fetchall()
        return {r["ref"] for r in rows}

    # --- write path ----------------------------------------------------- #

    def _schema_is_current(self) -> bool:
        if not self._schema_current:
            with self._lock:
                self._schema_current = self._conn.execute(
                    """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                                      WHERE table_schema = current_schema()
                                        AND table_name = 'fact_event'
                                        AND column_name = 'object_hash')
                          AND EXISTS (SELECT 1 FROM information_schema.tables
                                      WHERE table_schema = current_schema()
                                        AND table_name = 'projection_state') AS ok"""
                ).fetchone()["ok"]
        return self._schema_current

    def _head(self) -> LogPosition:
        """The last audit entry, or LOG_START when the log is empty."""
        row = self._conn.execute(
            "SELECT seq, entry_hash FROM audit_entry ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return LogPosition(row["seq"], row["entry_hash"]) if row else LOG_START

    def _on_chain(self, position: LogPosition) -> bool:
        # The entry hash commits to every entry before it, so a match means
        # `position` names exactly this log's first `seq` events.
        if position.seq == 0:
            return position.entry_hash == GENESIS
        row = self._conn.execute(
            "SELECT entry_hash FROM audit_entry WHERE seq = %s", (position.seq,)
        ).fetchone()
        return row is not None and row["entry_hash"] == position.entry_hash

    def _require_current_schema(self) -> None:
        # Writes need the current schema; reads tolerate an older one, so an
        # auditor can verify a log they can't migrate. Checked before anything
        # is written, so an unmigrated database fails cleanly rather than
        # midway through an add(); re-checked until found, so a migration
        # applied while the process runs is picked up.
        if not self._schema_is_current():
            raise RuntimeError(
                "the Postgres schema is older than this version of attestari; apply it "
                "again with `python -m attestari.initdb <dsn>` (it is idempotent)"
            )

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """Hold the event log for a block of appends: one transaction under the
        advisory lock, so they commit together and no other writer (thread,
        worker or process) appends in between. Re-entrant: the outermost block
        owns the transaction, and appends inside it join it."""
        with self._lock:
            if self._write_depth:
                self._write_depth += 1
                try:
                    yield
                finally:
                    self._write_depth -= 1
                return
            self._require_current_schema()
            with self._conn.transaction():
                self._conn.execute(_LOG_LOCK)
                self._write_depth = 1
                try:
                    yield
                finally:
                    self._write_depth = 0

    def _is_forgotten(self, subject_id: str) -> bool:
        # The tombstone keeps the forgotten subject id in `subject`.
        return (
            self._conn.execute(
                """SELECT 1 FROM fact_event
                   WHERE subject = %s AND op = 'subject_forgotten' LIMIT 1""",
                (subject_id,),
            ).fetchone()
            is not None
        )

    def append(self, event: Event) -> None:
        # One transaction per append (or the enclosing write_lock's): the event
        # row and its audit entry commit atomically, so a crash cannot leave the
        # chain out of step with the log, and the advisory lock serialises
        # appenders so two writers can never read the same prev_hash and fork
        # the chain.
        with self.write_lock():
            # Checked under the log lock, so a forget() from another worker
            # lands before this write or refuses it.
            sid = content_subject(event)
            if sid is not None and self._is_forgotten(sid):
                raise ForgottenSubjectError(sid)
            head = self._head()
            # Seal inside the transaction so a freshly minted DEK commits
            # atomically with the event it protects (KeyManager.seal).
            committed, stored = self._keys.seal(event)
            # The chain extension comes from audit.next_entry — shared with the
            # in-memory adapter, so the two chains cannot diverge. The entry's
            # seq is also stamped onto the event row (event_seq): one global
            # append order across episode + fact_event, which events() reads
            # back so deep verification sees the exact chained order.
            entry = next_entry(head.entry_hash, head.seq + 1, committed)
            self._insert_event(stored, entry.seq)
            self._conn.execute(
                """INSERT INTO audit_entry
                       (seq, kind, ref, payload_hash, prev_hash, entry_hash)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (entry.seq, entry.kind, entry.ref, entry.payload_hash,
                 entry.prev_hash, entry.entry_hash),
            )

    def _insert_event(self, event: Event, event_seq: int) -> None:
        # `event` arrives sealed: PII fields already ciphertext when encrypting.
        if isinstance(event, EpisodeIngested):
            self._conn.execute(
                """INSERT INTO episode
                       (episode_id, content_hash, payload, source_ref,
                        subject_id, agent_id, session_id, org_id, ingested_at, event_seq)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event.episode_id,
                    event.content_hash,
                    event.payload,
                    event.source_ref,
                    event.scope.subject_id,
                    event.scope.agent_id,
                    event.scope.session_id,
                    event.scope.org_id,
                    event.ingested_at,
                    event_seq,
                ),
            )
        elif isinstance(event, FactAsserted):
            lo, hi = event.char_span if event.char_span else (None, None)
            self._conn.execute(
                """INSERT INTO fact_event
                       (op, fact_id, subject, predicate, object, object_hash, confidence,
                        valid_from, valid_to, source_episode, char_span_lo, char_span_hi,
                        recorded_at, event_seq)
                   VALUES ('asserted', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event.fact_id,
                    event.subject,
                    event.predicate,
                    event.object,
                    event.object_hash,
                    event.confidence,
                    event.valid_from,
                    event.valid_to,
                    event.source_episode_id,
                    lo,
                    hi,
                    event.recorded_at,
                    event_seq,
                ),
            )
        elif isinstance(event, FactInvalidated):
            self._conn.execute(
                """INSERT INTO fact_event
                       (op, fact_id, reason, valid_to, superseded_by, recorded_at, event_seq)
                   VALUES ('invalidated', %s, %s, %s, %s, %s, %s)""",
                (event.fact_id, event.reason, event.valid_to, event.superseded_by,
                 event.recorded_at, event_seq),
            )
        elif isinstance(event, EntityMerged):
            self._conn.execute(
                """INSERT INTO fact_event (op, canonical_id, alias_id, reason, recorded_at, event_seq)
                   VALUES ('entity_merged', %s, %s, %s, %s, %s)""",
                (event.canonical_id, event.alias_id, event.evidence, event.recorded_at, event_seq),
            )
        elif isinstance(event, EntityUnmerged):
            self._conn.execute(
                """INSERT INTO fact_event (op, canonical_id, alias_id, recorded_at, event_seq)
                   VALUES ('entity_unmerged', %s, %s, %s, %s)""",
                (event.canonical_id, event.alias_id, event.recorded_at, event_seq),
            )
        elif isinstance(event, SubjectForgotten):
            # Reuse the `subject` column to hold the forgotten subject id.
            self._conn.execute(
                """INSERT INTO fact_event (op, subject, requested_by, recorded_at, event_seq)
                   VALUES ('subject_forgotten', %s, %s, %s, %s)""",
                (event.subject_id, event.requested_by, event.recorded_at, event_seq),
            )
        else:  # pragma: no cover - defensive
            raise TypeError(f"unknown event type: {type(event)!r}")

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

    # --- read path ------------------------------------------------------ #

    def events(self) -> list[Event]:
        return self._snapshot(self._read_events)

    def changes_since(self, position: LogPosition) -> tuple[LogPosition, list[Event]] | None:
        def read() -> tuple[LogPosition, list[Event]] | None:
            if not self._on_chain(position):
                return None
            return self._head(), self._read_events(after_seq=position.seq)

        return self._snapshot(read)

    def _snapshot(self, read):
        from psycopg import pq

        with self._lock:
            if self._conn.info.transaction_status != pq.TransactionStatus.IDLE:
                # Called inside a transaction (a write holding the log lock):
                # no append can land between the reads.
                return read()
            # Otherwise read every table from one snapshot. An append that
            # commits between the reads would yield a fact whose source episode
            # was never read, or events past the head this read reports.
            with self._conn.transaction():
                self._conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                return read()

    def _read_events(self, after_seq: int = 0) -> list[Event]:
        """Events in the exact append order the audit chain committed, from
        after `after_seq`. event_seq (= the audit entry's seq) is a total order
        across the episode and fact_event tables, so deep verification aligns
        1:1. A full read (`after_seq=0`) also takes rows from before event_seq
        existed, which have none."""
        ordered: list[tuple[int, Event]] = []
        encrypted = self.cipher.enabled
        since, params = ("", None) if after_seq == 0 else ("WHERE {}event_seq > %s", (after_seq,))

        episodes = self._conn.execute(
            f"SELECT * FROM episode {since.format('')} ORDER BY event_seq", params
        ).fetchall()
        # A fact's scope is its source episode's, which a read from after_seq
        # may not include, so it comes with the fact.
        facts = self._conn.execute(
            f"""SELECT f.*, e.episode_id IS NOT NULL AS has_episode,
                       e.subject_id AS ep_subject_id, e.agent_id AS ep_agent_id,
                       e.session_id AS ep_session_id, e.org_id AS ep_org_id
                  FROM fact_event f LEFT JOIN episode e ON e.episode_id = f.source_episode
                  {since.format('f.')}
                 ORDER BY f.event_seq, f.seq""",
            params,
        ).fetchall()
        deks = self._keys.deks_for(  # {} when encryption is disabled
            {r["subject_id"] for r in episodes if r["subject_id"]}
            | {r["ep_subject_id"] for r in facts if r["ep_subject_id"]}
        )

        for row in episodes:
            subject_id = row["subject_id"]
            payload = row["payload"] or ""
            if encrypted and subject_id:
                if subject_id not in deks:
                    continue  # DEK destroyed -> subject erased: the episode is unreadable
                payload = self.cipher.decrypt(deks[subject_id], payload)
            eid = str(row["episode_id"])
            scope = Scope(
                subject_id=subject_id,
                agent_id=row["agent_id"],
                session_id=row["session_id"],
                org_id=row["org_id"],
            )
            ordered.append(
                (
                    row["event_seq"] or 0,
                    EpisodeIngested(
                        episode_id=eid,
                        content_hash=row["content_hash"],
                        payload=payload,
                        scope=scope,
                        ingested_at=row["ingested_at"],
                        source_ref=row["source_ref"],
                    ),
                )
            )

        for row in facts:
            op = row["op"]
            eseq = row["event_seq"] or 0
            if op == "asserted":
                src = str(row["source_episode"]) if row["source_episode"] else ""
                scope = (
                    Scope(
                        subject_id=row["ep_subject_id"],
                        agent_id=row["ep_agent_id"],
                        session_id=row["ep_session_id"],
                        org_id=row["ep_org_id"],
                    )
                    if row["has_episode"]
                    else Scope()
                )
                obj = row["object"]
                if encrypted:
                    if not row["has_episode"]:
                        continue  # no source episode to read the fact under
                    if scope.subject_id:
                        if scope.subject_id not in deks:
                            continue  # source episode erased -> the fact is erased too
                        obj = self.cipher.decrypt(deks[scope.subject_id], obj)
                ordered.append(
                    (
                        eseq,
                        FactAsserted(
                            fact_id=str(row["fact_id"]),
                            subject=row["subject"],
                            predicate=row["predicate"],
                            object=obj,
                            source_episode_id=src,
                            valid_from=row["valid_from"],
                            valid_to=row["valid_to"],
                            confidence=row["confidence"],
                            char_span=_span(row["char_span_lo"], row["char_span_hi"]),
                            scope=scope,
                            recorded_at=row["recorded_at"],
                            # .get: read-only use still works on an unmigrated schema
                            object_hash=row.get("object_hash"),
                        ),
                    )
                )
            elif op == "invalidated":
                ordered.append(
                    (
                        eseq,
                        FactInvalidated(
                            fact_id=str(row["fact_id"]),
                            reason=row["reason"] or "",
                            valid_to=row["valid_to"],
                            superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
                            recorded_at=row["recorded_at"],
                        ),
                    )
                )
            elif op == "entity_merged":
                ordered.append(
                    (
                        eseq,
                        EntityMerged(
                            canonical_id=row["canonical_id"],
                            alias_id=row["alias_id"],
                            evidence=row["reason"] or "",
                            recorded_at=row["recorded_at"],
                        ),
                    )
                )
            elif op == "entity_unmerged":
                ordered.append(
                    (
                        eseq,
                        EntityUnmerged(
                            canonical_id=row["canonical_id"],
                            alias_id=row["alias_id"],
                            recorded_at=row["recorded_at"],
                        ),
                    )
                )
            elif op == "subject_forgotten":
                ordered.append(
                    (
                        eseq,
                        SubjectForgotten(
                            subject_id=row["subject"],
                            requested_by=row["requested_by"] or "system",
                            recorded_at=row["recorded_at"],
                        ),
                    )
                )
        ordered.sort(key=lambda p: p[0])
        return [ev for _, ev in ordered]

    # --- helpers -------------------------------------------------------- #

    def truncate(self) -> None:
        """Wipe all events and projections (test/dev helper)."""
        with self._lock:
            self._conn.execute(
                "TRUNCATE episode, fact_event, entity, edge, projection_state, "
                "deletion_certificate, keyring, audit_entry CASCADE"
            )
            self._keys.reset_cache()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresProjectionBackend:
    """Materialised-projection backend.

    Keeps the `entity`/`edge` projection tables in step with the durable event
    log and serves hybrid retrieval in SQL: pgvector cosine similarity +
    Postgres full-text ranking + a bi-temporal `as_of` filter. This is what
    lights up the HNSW index in src/attestari/db/schema.sql. `project()` serves
    timeline/supersession from the in-process projection (see ProjectionCache);
    only `search()` reads the materialised tables.

    A write updates only the rows its events touched, in the write's own
    transaction, so the tables and the log commit together. `projection_state`
    records the audit entry the tables reflect; when that row is missing or no
    longer on the chain, the tables are rebuilt from the log.
    """

    def __init__(self, store: PostgresEventStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder
        self._projector = Projector(embedder)
        self._cache = ProjectionCache(store, self._projector)
        self._conn = store._conn
        self._lock = store._lock  # same connection, so the same lock
        self._dim = getattr(embedder, "dim", 384)
        self._sync_on_attach()

    def _sync_on_attach(self) -> None:
        # Bring the tables up to the log once. On an older schema, reads keep
        # working from the tables as they are, and writes ask for initdb.
        if not self.store._schema_is_current():
            return
        with self.store.write_lock():
            emptied = self._conn.execute(
                """SELECT NOT EXISTS (SELECT 1 FROM edge)
                      AND EXISTS (SELECT 1 FROM fact_event WHERE op = 'asserted') AS emptied"""
            ).fetchone()["emptied"]
            if emptied:
                # Tables emptied behind the state row, e.g. restored from a
                # backup that left the derived tables out.
                self._rebuild_tables()
            else:
                self._sync()

    def project(self) -> Projection:
        return self._cache.current()

    def on_write(self) -> None:
        # Memory calls this inside its write lock, so the update commits with
        # the events it reflects.
        with self.store.write_lock():
            self._sync()

    def on_forget(self, certificate: DeletionCertificate) -> None:
        # Crypto-shred: destroy the subject's DEK so all their ciphertext at rest
        # becomes unrecoverable. The subject's materialised rows are dropped by the
        # rebuild in on_write (the fold then skips the unreadable subject).
        # Shred and certificate commit together: no destroyed key without its
        # proof. Inside forget()'s write lock, they also commit with the tombstone.
        with self.store.write_lock():
            self.store.shred_subject(certificate.subject_id)
            # Persist the certificate (the proof retained after the content is gone).
            self._conn.execute(
                """INSERT INTO deletion_certificate
                       (certificate_id, subject_id, requested_by,
                        episodes_count, facts_count, manifest_hash, issued_at,
                        signature, algorithm)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    certificate.certificate_id,
                    certificate.subject_id,
                    certificate.requested_by,
                    certificate.episodes_deleted,
                    certificate.facts_deleted,
                    certificate.manifest_hash,
                    certificate.issued_at,
                    certificate.signature,
                    certificate.algorithm,
                ),
            )

    def certificates(self) -> list[DeletionCertificate]:
        """Read back the persisted deletion certificates, oldest first.

        This is the tier's certificate register — what an evidence bundle (or an
        auditor with read access) lists without needing the original caller to
        have kept their copy from `forget()`."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT certificate_id, subject_id, requested_by,
                          episodes_count, facts_count, manifest_hash, issued_at,
                          signature, algorithm
                     FROM deletion_certificate ORDER BY issued_at"""
            ).fetchall()
        return [
            DeletionCertificate(
                certificate_id=str(r["certificate_id"]),
                subject_id=r["subject_id"],
                requested_by=r["requested_by"],
                episodes_deleted=r["episodes_count"],
                facts_deleted=r["facts_count"],
                manifest_hash=r["manifest_hash"],
                issued_at=r["issued_at"],
                signature=r["signature"],
                algorithm=r["algorithm"],
            )
            for r in rows
        ]

    def rebuild(self) -> None:
        """Materialise entity + edge tables from the whole event log.

        Writes keep the tables current on their own (see on_write); this is the
        fallback, and the way to start over. One transaction under the
        event-log lock, so it serialises with appends and with other writers.
        TRUNCATE keeps its table lock until commit, so a concurrent search waits
        for the new projection rather than reading a half-built one.
        """
        with self.store.write_lock():
            self._rebuild_tables()

    # --- keeping the tables in step with the log (under the write lock) --- #

    def _sync(self) -> None:
        """Bring the tables up to the head of the log: only the rows touched by
        events after `projection_state`, or everything when that position is
        missing or no longer on the chain."""
        head = self.store._head()
        row = self._conn.execute(
            "SELECT last_seq, last_hash FROM projection_state"
        ).fetchone()
        state = LogPosition(row["last_seq"], row["last_hash"]) if row else None
        if state == head:
            return
        if state is None or not self.store._on_chain(state):
            self._rebuild_tables()
            return
        self._apply_since(state.seq)
        self._set_state(head)

    def _apply_since(self, seq: int) -> None:
        # Which rows the new events can change. Episodes change none; a fact's
        # assertion or invalidation changes its edge; an assertion, merge or
        # unmerge changes an entity; a forget drops the subject's edges and the
        # entity named after them.
        rows = self._conn.execute(
            """SELECT op, fact_id::text AS fact_id, subject, canonical_id
                 FROM fact_event WHERE event_seq > %s""",
            (seq,),
        ).fetchall()
        if not rows:
            return
        facts: set[str] = set()
        entities: set[str] = set()
        forgotten: set[str] = set()
        for r in rows:
            if r["op"] in ("asserted", "invalidated"):
                facts.add(r["fact_id"])
            if r["op"] == "asserted":
                entities.add(r["subject"])
            elif r["op"] in ("entity_merged", "entity_unmerged"):
                entities.add(r["canonical_id"])
            elif r["op"] == "subject_forgotten":
                forgotten.add(r["subject"])
                entities.add(r["subject"])

        # The rows take their values from the projection, as in a rebuild.
        proj = self._cache.current()
        if forgotten:
            self._conn.execute(
                "DELETE FROM edge WHERE subject_id = ANY(%s)", (sorted(forgotten),)
            )
        gone = sorted(f for f in facts if f not in proj.edges)
        if gone:
            self._conn.execute("DELETE FROM edge WHERE fact_id = ANY(%s::uuid[])", (gone,))
        self._write_edges([proj.edges[f] for f in sorted(facts) if f in proj.edges])
        gone = sorted(c for c in entities if c not in proj.entities)
        if gone:
            self._conn.execute("DELETE FROM entity WHERE canonical_id = ANY(%s)", (gone,))
        self._write_entities([proj.entities[c] for c in sorted(entities) if c in proj.entities])

    def _rebuild_tables(self) -> None:
        proj = self._projector.build(self.store.events())
        self._conn.execute("TRUNCATE entity, edge")
        self._write_entities(list(proj.entities.values()))
        self._write_edges(list(proj.edges.values()))
        self._set_state(self.store._head())

    def _write_entities(self, entities: list[Entity]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO entity (canonical_id, aliases) VALUES (%s, %s)
                   ON CONFLICT (canonical_id) DO UPDATE SET aliases = EXCLUDED.aliases""",
                [(ent.canonical_id, sorted(ent.aliases)) for ent in entities],
            )

    def _write_edges(self, edges: list[Edge]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO edge
                       (fact_id, subject, predicate, object, valid_from, valid_to,
                        tx_from, tx_to, confidence, source_episode, char_span_lo, char_span_hi,
                        subject_id, alive, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
                   ON CONFLICT (fact_id) DO UPDATE SET
                       subject = EXCLUDED.subject, predicate = EXCLUDED.predicate,
                       object = EXCLUDED.object, valid_from = EXCLUDED.valid_from,
                       valid_to = EXCLUDED.valid_to, tx_from = EXCLUDED.tx_from,
                       tx_to = EXCLUDED.tx_to, confidence = EXCLUDED.confidence,
                       source_episode = EXCLUDED.source_episode,
                       char_span_lo = EXCLUDED.char_span_lo,
                       char_span_hi = EXCLUDED.char_span_hi,
                       subject_id = EXCLUDED.subject_id, alive = EXCLUDED.alive,
                       embedding = EXCLUDED.embedding""",
                [
                    (
                        e.fact_id, e.subject, e.predicate, e.object, e.valid_from, e.valid_to,
                        e.tx_from, e.tx_to, e.confidence, e.source_episode_id or None,
                        e.char_span[0] if e.char_span else None,
                        e.char_span[1] if e.char_span else None,
                        e.subject_id, e.alive, _vec_literal(e.embedding),
                    )
                    for e in edges
                ],
            )

    def _set_state(self, head: LogPosition) -> None:
        self._conn.execute(
            """INSERT INTO projection_state (singleton, last_seq, last_hash)
               VALUES (TRUE, %s, %s)
               ON CONFLICT (singleton)
               DO UPDATE SET last_seq = EXCLUDED.last_seq, last_hash = EXCLUDED.last_hash""",
            (head.seq, head.entry_hash),
        )

    def search(
        self,
        query: str,
        *,
        subject_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> list[SearchResult]:
        # Same weighted blend + tie-breaks as the in-memory retriever (weights
        # imported from retrieve.py — single source of truth), so a query ranks
        # identically regardless of deployment mode.
        sem_w, kw_w = weights_for(self.embedder)
        params: dict[str, object] = {
            "qvec": _vec_literal(self.embedder.embed(query)),
            "q": query,
            "limit": limit,
            "sem_w": sem_w,
            "kw_w": kw_w,
        }
        where = []
        if as_of is not None:
            where.append("valid_from <= %(as_of)s AND (valid_to IS NULL OR valid_to > %(as_of)s)")
            params["as_of"] = as_of
        else:
            where.append("alive = TRUE")
        if subject_id is not None:
            where.append("subject_id = %(subject_id)s")
            params["subject_id"] = subject_id

        sql = f"""
            WITH scored AS (
                SELECT *,
                    (1 - (embedding <=> %(qvec)s::vector)) AS sem,
                    COALESCE(ts_rank(
                        to_tsvector('english',
                            subject || ' ' || replace(predicate, '_', ' ') || ' ' || object),
                        -- OR the query terms (plainto_tsquery ANDs them, which is
                        -- too strict for partial-match ranking).
                        to_tsquery('english',
                            NULLIF(replace(plainto_tsquery('english', %(q)s)::text, '&', '|'), ''))
                    ), 0) AS kw
                FROM edge
                WHERE {" AND ".join(where)}
            )
            -- LEAST(kw*10, 1) squashes ts_rank's small values into the same
            -- 0..1 range as the in-memory overlap ratio before blending.
            -- Ordering mirrors retrieve.search exactly: score desc, then
            -- earliest-established (tx_from) wins exact ties, then fact_id.
            SELECT *, (%(sem_w)s * sem + %(kw_w)s * LEAST(kw * 10, 1.0)) AS score
            FROM scored
            ORDER BY score DESC, tx_from ASC, fact_id
            LIMIT %(limit)s
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results: list[SearchResult] = []
        for row in rows:
            edge = Edge(
                fact_id=str(row["fact_id"]),
                subject=row["subject"],
                predicate=row["predicate"],
                object=row["object"],
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                tx_from=row["tx_from"],
                tx_to=row["tx_to"],
                confidence=row["confidence"],
                source_episode_id=str(row["source_episode"]) if row["source_episode"] else "",
                char_span=_span(row["char_span_lo"], row["char_span_hi"]),
                subject_id=row["subject_id"],
                alive=row["alive"],
                embedding=[],
            )
            results.append(SearchResult(edge=edge, score=float(row["score"])))
        return results
