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
from .projection import Edge, Projection, Projector
from .records import DeletionCertificate
from .retrieve import SearchResult, weights_for

_DEFAULT_DSN = "postgresql://attestari:attestari@localhost:5432/attestari"


def _span(lo: int | None, hi: int | None) -> tuple[int, int] | None:
    return (lo, hi) if lo is not None and hi is not None else None


def _vec_literal(vec: list[float]) -> str:
    """pgvector text literal, e.g. '[0.1,0.2,...]' (cast to ::vector in SQL)."""
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


class _PostgresKeyring:
    """Keyring adapter over the `keyring` table (wrapped DEKs at rest)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def get(self, subject_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT wrapped_dek FROM keyring WHERE subject_id = %s", (subject_id,)
        ).fetchone()
        return bytes(row["wrapped_dek"]) if row is not None else None

    def put(self, subject_id: str, wrapped_dek: bytes) -> None:
        self._conn.execute(
            "INSERT INTO keyring (subject_id, wrapped_dek) VALUES (%s, %s)",
            (subject_id, wrapped_dek),
        )

    def delete(self, subject_id: str) -> None:
        self._conn.execute("DELETE FROM keyring WHERE subject_id = %s", (subject_id,))

    def all(self) -> dict[str, bytes]:
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
        # Encryption is opt-in: EnvelopeCipher when ATTESTARI_KEK is set, else NullCipher.
        self.cipher = cipher or cipher_from_env()
        # Key lifecycle is delegated to the shared KeyManager; only the resting
        # place of the wrapped DEKs (the keyring table) is adapter-specific.
        self._keys = KeyManager(self.cipher, _PostgresKeyring(self._conn))

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

    def append(self, event: Event) -> None:
        # One transaction per append: the event row and its audit entry commit
        # atomically (a crash cannot leave the chain out of step with the log),
        # and an advisory xact lock serialises concurrent appenders so two
        # writers can never read the same prev_hash and fork the chain.
        with self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('attestari.event_log'))")
            row = self._conn.execute(
                "SELECT seq, entry_hash FROM audit_entry ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev = row["entry_hash"] if row else GENESIS
            # The chain extension comes from audit.next_entry — shared with the
            # in-memory adapter, so the two chains cannot diverge. The entry's
            # seq is also stamped onto the event row (event_seq): one global
            # append order across episode + fact_event, which events() reads
            # back so deep verification sees the exact chained order.
            entry = next_entry(prev, (row["seq"] + 1) if row else 1, event)
            self._insert_event(event, entry.seq)
            self._conn.execute(
                """INSERT INTO audit_entry (seq, kind, ref, payload_hash, prev_hash, entry_hash)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (entry.seq, entry.kind, entry.ref, entry.payload_hash, entry.prev_hash, entry.entry_hash),
            )

    def _insert_event(self, event: Event, event_seq: int) -> None:
        if isinstance(event, EpisodeIngested):
            payload = event.payload
            if self.cipher.enabled and event.scope.subject_id:
                payload = self._keys.encrypt_for(event.scope.subject_id, payload)
            self._conn.execute(
                """INSERT INTO episode
                       (episode_id, content_hash, payload, source_ref,
                        subject_id, agent_id, session_id, org_id, ingested_at, event_seq)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event.episode_id,
                    event.content_hash,
                    payload,
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
            obj = event.object
            if self.cipher.enabled and event.scope.subject_id:
                obj = self._keys.encrypt_for(event.scope.subject_id, obj)
            self._conn.execute(
                """INSERT INTO fact_event
                       (op, fact_id, subject, predicate, object, confidence,
                        valid_from, valid_to, source_episode, char_span_lo, char_span_hi,
                        recorded_at, event_seq)
                   VALUES ('asserted', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event.fact_id,
                    event.subject,
                    event.predicate,
                    obj,
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
        # Reconstruct in the exact append order the audit chain committed:
        # event_seq (= the audit entry's seq) is a total order across the
        # episode and fact_event tables, so deep verification aligns 1:1.
        ordered: list[tuple[int, Event]] = []
        scope_by_episode: dict[str, Scope] = {}
        encrypted = self.cipher.enabled
        deks = self._keys.live_deks()  # {} when encryption is disabled

        episodes = self._conn.execute(
            "SELECT * FROM episode ORDER BY event_seq"
        ).fetchall()
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
            scope_by_episode[eid] = scope
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

        facts = self._conn.execute("SELECT * FROM fact_event ORDER BY event_seq, seq").fetchall()
        for row in facts:
            op = row["op"]
            eseq = row["event_seq"] or 0
            if op == "asserted":
                src = str(row["source_episode"]) if row["source_episode"] else ""
                obj = row["object"]
                if encrypted:
                    if src not in scope_by_episode:
                        continue  # source episode erased -> the fact is erased too
                    sid = scope_by_episode[src].subject_id
                    if sid in deks:
                        obj = self.cipher.decrypt(deks[sid], obj)
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
                            scope=scope_by_episode.get(src, Scope()),
                            recorded_at=row["recorded_at"],
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
        self._conn.execute(
            "TRUNCATE episode, fact_event, entity, edge, deletion_certificate, keyring, audit_entry CASCADE"
        )
        self._keys.reset_cache()

    def close(self) -> None:
        self._conn.close()


class PostgresProjectionBackend:
    """Materialised-projection backend.

    Rebuilds the `entity`/`edge` projection tables from the durable event log and
    serves hybrid retrieval in SQL: pgvector cosine similarity + Postgres
    full-text ranking + a bi-temporal `as_of` filter. This is what lights up the
    HNSW index in src/attestari/db/schema.sql. `project()` still folds the log for
    timeline/supersession (the log is the source of truth); only `search()` reads
    the materialised table.

    Rebuild-on-write is O(n) and fine for now; incremental projection is a
    later optimisation.
    """

    def __init__(self, store: PostgresEventStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder
        self._projector = Projector(embedder)
        self._conn = store._conn
        self._dim = getattr(embedder, "dim", 384)
        self._maybe_initial_rebuild()

    def _maybe_initial_rebuild(self) -> None:
        # If we attach to a log that hasn't been materialised yet, build it once.
        edge_n = self._conn.execute("SELECT count(*) AS n FROM edge").fetchone()["n"]
        fe_n = self._conn.execute("SELECT count(*) AS n FROM fact_event").fetchone()["n"]
        if edge_n == 0 and fe_n > 0:
            self.rebuild()

    def project(self) -> Projection:
        return self._projector.build(self.store.events())

    def on_write(self) -> None:
        self.rebuild()

    def on_forget(self, certificate: DeletionCertificate) -> None:
        # Crypto-shred: destroy the subject's DEK so all their ciphertext at rest
        # becomes unrecoverable. The subject's materialised rows are dropped by the
        # rebuild in on_write (the fold then skips the unreadable subject).
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

    def rebuild(self) -> None:
        """Materialise entity + edge tables from the event log (rebuildable)."""
        proj = self._projector.build(self.store.events())
        conn = self._conn
        conn.execute("TRUNCATE entity, edge")
        for ent in proj.entities.values():
            conn.execute(
                "INSERT INTO entity (canonical_id, aliases) VALUES (%s, %s)",
                (ent.canonical_id, list(ent.aliases)),
            )
        for e in proj.edges.values():
            lo = e.char_span[0] if e.char_span else None
            hi = e.char_span[1] if e.char_span else None
            conn.execute(
                """INSERT INTO edge
                       (fact_id, subject, predicate, object, valid_from, valid_to,
                        tx_from, tx_to, confidence, source_episode, char_span_lo, char_span_hi,
                        subject_id, alive, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)""",
                (
                    e.fact_id, e.subject, e.predicate, e.object, e.valid_from, e.valid_to,
                    e.tx_from, e.tx_to, e.confidence, e.source_episode_id or None, lo, hi,
                    e.subject_id, e.alive, _vec_literal(e.embedding),
                ),
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
