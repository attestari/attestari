"""Postgres adapter integration tests (materialised projections + SQL retrieval).

Skipped unless ATTESTARI_DATABASE_URL points at a live Postgres (the
docker-compose one). These exercise the real pgvector + full-text retrieval path
via Memory.postgres(), durability across fresh engines, projection
materialisation, forget, and certificate persistence.

    ATTESTARI_PG_PORT=5433 docker compose up -d
    ATTESTARI_DATABASE_URL=postgresql://attestari:attestari@localhost:5433/attestari \
        python -m pytest tests/test_postgres.py -q
"""

from __future__ import annotations

import os

import pytest

from attestari import Memory

DSN = os.environ.get("ATTESTARI_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set ATTESTARI_DATABASE_URL to run Postgres tests")


def _reset() -> None:
    from attestari import PostgresEventStore

    store = PostgresEventStore(DSN)
    store.truncate()
    store.close()


def test_postgres_bitemporal_and_persistence() -> None:
    _reset()
    mem = Memory.postgres(DSN)
    mem.add(
        "Hi, my name is Dana. I live in Delhi and I work at Acme.",
        subject_id="u1",
        valid_from="2019-01-01",
        source_ref="m1",
    )
    mem.add(
        "I moved to Berlin and I joined Globex.",
        subject_id="u1",
        valid_from="2026-03-01",
        source_ref="m2",
    )

    # Hybrid SQL retrieval (pgvector + full-text) + bi-temporal as-of.
    assert mem.answer("where does the user live", subject_id="u1") == "Berlin"
    assert mem.answer("where did the user live", subject_id="u1", as_of="2020-01-01") == "Delhi"
    assert mem.answer("where does the user work", subject_id="u1") == "Globex"

    # Durability: a brand-new engine (fresh connection) reads the materialised
    # projection and the durable log for provenance.
    mem2 = Memory.postgres(DSN)
    assert mem2.answer("where does the user live", subject_id="u1") == "Berlin"
    top = mem2.search("where does the user live", subject_id="u1")[0]
    prov = mem2.get_provenance(top.edge.fact_id)
    assert prov is not None and prov.snippet == "Berlin" and prov.source_ref == "m2"


def test_postgres_materializes_edges_with_embeddings() -> None:
    _reset()
    mem = Memory.postgres(DSN)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")

    from attestari import PostgresEventStore

    store = PostgresEventStore(DSN)
    n = store._conn.execute(
        "SELECT count(*) AS n FROM edge WHERE embedding IS NOT NULL"
    ).fetchone()["n"]
    store.close()
    assert n >= 2  # name_is + lives_in materialised with pgvector embeddings


def test_postgres_forget_persists_certificate_and_isolates() -> None:
    _reset()
    mem = Memory.postgres(DSN)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2", valid_from="2021-01-01")

    cert = mem.forget("u1", requested_by="dpo@example.com")
    assert cert.episodes_deleted == 1
    assert cert.facts_deleted >= 2

    # The certificate is persisted (proof retained).
    from attestari import PostgresEventStore

    store = PostgresEventStore(DSN)
    row = store._conn.execute(
        "SELECT subject_id, facts_count FROM deletion_certificate WHERE certificate_id = %s",
        (cert.certificate_id,),
    ).fetchone()
    store.close()
    assert row is not None and row["subject_id"] == "u1"

    # u1 dropped from the materialised projection; u2 untouched.
    mem2 = Memory.postgres(DSN)
    assert mem2.answer("where does the user live", subject_id="u1") is None
    assert mem2.answer("where does the user live", subject_id="u2") == "Chennai"


def test_postgres_evidence_bundle_lists_the_certificate_register() -> None:
    """On the tier that persists certificates, the evidence bundle reads them
    back — an auditor doesn't depend on whoever called forget() keeping their
    copy. (Other tiers report `certificates: None`; see tests/test_evidence.py.)"""
    from attestari.evidence import build_evidence

    _reset()
    mem = Memory.postgres(DSN)
    mem.add("My name is Dana. I live in Delhi.", subject_id="u1", valid_from="2019-01-01")
    mem.add("I'm Ravi. I live in Chennai.", subject_id="u2", valid_from="2021-01-01")
    mem.forget("u1", requested_by="dpo@example.com")

    bundle = build_evidence(mem, deep=True)
    assert bundle["ok"] is True
    assert [row["subject_id"] for row in bundle["erasures"]] == ["u1"]

    certs = bundle["certificates"]
    assert certs is not None and len(certs) == 1
    assert certs[0]["subject_id"] == "u1"
    assert certs[0]["requested_by"] == "dpo@example.com"
    assert certs[0]["facts_deleted"] >= 2


def test_initdb_is_packaged_and_idempotent() -> None:
    """The schema ships inside the package (pip-only users need no clone), and
    applying it to an already-initialised database is a no-op, not an error."""
    from attestari.initdb import init_db, schema_sql

    sql = schema_sql()
    assert "CREATE TABLE IF NOT EXISTS episode" in sql
    assert "event_seq" in sql  # includes the lightweight migrations

    _reset()
    init_db(DSN)  # DB already has the schema — must be idempotent
    mem = Memory.postgres(DSN)
    mem.add("Hi, I'm Alice. I live in Berlin.", subject_id="u1")
    assert mem.answer("where does the user live", subject_id="u1") == "Berlin"
    _reset()
