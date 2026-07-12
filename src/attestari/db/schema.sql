-- Attestari — Postgres schema (the durable core)
--
-- This is the single-database core: the event log is the source of truth, and
-- the graph / vector / full-text projections are rebuildable from it. The whole
-- engine runs on one Postgres + pgvector container — no graph database required.
--
-- Bi-temporal modelling is explicit on every fact:
--   * valid_from / valid_to  -> VALID time  (when the fact is true in the world)
--   * tx_from   / tx_to       -> SYSTEM time (when Attestari knew it)
-- "What did the agent believe about X on date D, and why?" is a query against
-- these ranges plus the provenance link back to the source episode.
--
-- The spike implements this same model in-memory (see src/attestari/) so the
-- engine also runs with zero dependencies.

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector: HNSW semantic search
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram / keyword search fallback

-- ---------------------------------------------------------------------------
-- EVENT LOG  (append-only; the source of truth)
-- ---------------------------------------------------------------------------

-- Episodes: raw ingested material. Every derived fact traces back to one.
-- In production the raw payload is encrypted with a per-subject key (see the
-- `forget` flow); `content_hash` makes provenance tamper-evident.
CREATE TABLE IF NOT EXISTS episode (
    episode_id    UUID PRIMARY KEY,
    content_hash  TEXT        NOT NULL,            -- sha256 of raw payload
    payload       TEXT,                            -- raw text (or NULL once crypto-shredded)
    source_ref    TEXT,                            -- where it came from (doc id, message id, ...)
    subject_id    TEXT,                            -- scope: the data subject (e.g. end user)
    agent_id      TEXT,
    session_id    TEXT,
    org_id        TEXT,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_seq     BIGINT                           -- global append order (= audit_entry.seq)
);
CREATE INDEX IF NOT EXISTS episode_subject_idx ON episode (subject_id);

-- The fact event stream. Append-only: corrections are new rows, not updates.
-- `op` is the event kind; projections fold this stream into current state.
CREATE TABLE IF NOT EXISTS fact_event (
    seq            BIGSERIAL PRIMARY KEY,          -- total order of events
    op             TEXT        NOT NULL            -- asserted | invalidated | entity_merged | entity_unmerged | subject_forgotten
                       CHECK (op IN ('asserted','invalidated','entity_merged','entity_unmerged','subject_forgotten')),
    fact_id        UUID,
    subject        TEXT,                           -- entity id (canonical or alias at assert time)
    predicate      TEXT,
    object         TEXT,
    confidence     REAL,
    valid_from     TIMESTAMPTZ,                    -- VALID time: true-in-world start
    valid_to       TIMESTAMPTZ,                    -- VALID time: true-in-world end (NULL = still true)
    source_episode UUID REFERENCES episode (episode_id),
    char_span_lo   INT,
    char_span_hi   INT,
    -- entity_merged payload
    canonical_id   TEXT,
    alias_id       TEXT,
    -- bookkeeping
    reason         TEXT,                           -- why invalidated / forgotten / merged
    superseded_by  UUID,
    requested_by   TEXT,                           -- subject_forgotten: who asked
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- SYSTEM/transaction time
    event_seq      BIGINT                          -- global append order (= audit_entry.seq)
);
CREATE INDEX IF NOT EXISTS fact_event_fact_idx    ON fact_event (fact_id);
CREATE INDEX IF NOT EXISTS fact_event_subject_idx ON fact_event (subject);

-- Lightweight, idempotent migration for databases created before event_seq:
-- the shared append order across episode + fact_event that lets events() be
-- reconstructed in the exact order the audit chain committed.
ALTER TABLE episode    ADD COLUMN IF NOT EXISTS event_seq BIGINT;
ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS event_seq BIGINT;
CREATE INDEX IF NOT EXISTS episode_event_seq_idx    ON episode (event_seq);
CREATE INDEX IF NOT EXISTS fact_event_event_seq_idx ON fact_event (event_seq);

-- Deletion certificates: the proof retained AFTER a subject's data is destroyed.
-- We keep the certificate (that erasure happened, by whom, how much) and shred
-- the content. This is the GDPR Article 17 / EU AI Act audit surface.
CREATE TABLE IF NOT EXISTS deletion_certificate (
    certificate_id UUID PRIMARY KEY,
    subject_id     TEXT        NOT NULL,
    requested_by   TEXT        NOT NULL,
    episodes_count INT         NOT NULL,
    facts_count    INT         NOT NULL,
    manifest_hash  TEXT        NOT NULL,           -- hash of what was deleted (content destroyed)
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    signature      TEXT,                           -- HMAC-SHA256 under a KEK-derived key (NULL = issued unsigned)
    algorithm      TEXT                            -- e.g. 'hmac-sha256-kek-v1'
);
-- Idempotent migration for databases created before certificate signing:
ALTER TABLE deletion_certificate ADD COLUMN IF NOT EXISTS signature TEXT;
ALTER TABLE deletion_certificate ADD COLUMN IF NOT EXISTS algorithm TEXT;

-- Per-subject data-encryption keys, wrapped under the root KEK (crypto-shred).
-- PII at rest (episode.payload, fact object) is encrypted with the subject's DEK.
-- To forget a subject we DELETE this row — destroying the key makes all of that
-- subject's ciphertext unrecoverable, while the event rows remain for audit.
CREATE TABLE IF NOT EXISTS keyring (
    subject_id  TEXT PRIMARY KEY,
    wrapped_dek BYTEA       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tamper-evident audit chain: one row per appended event, hash-linked to the
-- previous (entry_hash = H(prev_hash || payload_hash)). Holds only content
-- digests (no raw PII), so it survives crypto-shred and proves the log's integrity.
CREATE TABLE IF NOT EXISTS audit_entry (
    seq          BIGSERIAL PRIMARY KEY,
    kind         TEXT        NOT NULL,
    ref          TEXT        NOT NULL,
    payload_hash TEXT        NOT NULL,
    prev_hash    TEXT        NOT NULL,
    entry_hash   TEXT        NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- PROJECTIONS  (derived, rebuildable from the event log above)
-- ---------------------------------------------------------------------------

-- Resolved entities. `aliases` records the surface forms merged into this node
-- ("Jane Doe" / "the founder" / "J.D." -> one canonical id).
CREATE TABLE IF NOT EXISTS entity (
    canonical_id  TEXT PRIMARY KEY,
    entity_type   TEXT,
    summary       TEXT,
    aliases       TEXT[]      NOT NULL DEFAULT '{}',
    embedding     vector(384)
);

-- The temporal knowledge graph edge — one row per (currently materialised) fact.
-- alive = the fact is valid as of "now" (valid_to IS NULL and not tombstoned).
CREATE TABLE IF NOT EXISTS edge (
    fact_id        UUID PRIMARY KEY,
    subject        TEXT        NOT NULL,
    predicate      TEXT        NOT NULL,
    object         TEXT        NOT NULL,
    valid_from     TIMESTAMPTZ NOT NULL,
    valid_to       TIMESTAMPTZ,                    -- NULL = still true in the world
    tx_from        TIMESTAMPTZ NOT NULL,
    tx_to          TIMESTAMPTZ,                    -- NULL = still the current record
    confidence     REAL        NOT NULL DEFAULT 1.0,
    source_episode UUID        REFERENCES episode (episode_id),
    char_span_lo   INT,
    char_span_hi   INT,
    subject_id     TEXT,                           -- scope, copied for deletion + filtering
    alive          BOOLEAN     NOT NULL DEFAULT TRUE,
    embedding      vector(384)                     -- embedding of the fact text
);
CREATE INDEX IF NOT EXISTS edge_subject_idx ON edge (subject);
CREATE INDEX IF NOT EXISTS edge_alive_idx   ON edge (alive);
-- Semantic search over facts (HNSW over cosine distance):
CREATE INDEX IF NOT EXISTS edge_embedding_idx ON edge USING hnsw (embedding vector_cosine_ops);
-- Keyword search over the fact text:
CREATE INDEX IF NOT EXISTS edge_text_trgm_idx ON edge USING gin ((subject || ' ' || predicate || ' ' || object) gin_trgm_ops);
