"""FastAPI server tests (in-memory engine via TestClient).

Skipped unless fastapi + httpx are installed (the `server`/`dev` extras).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from attestari import Memory  # noqa: E402
from attestari.server import create_app  # noqa: E402


def _client() -> TestClient:
    return TestClient(create_app(Memory()))


def test_healthz() -> None:
    assert _client().get("/healthz").json()["status"] == "ok"


def test_add_search_timeline_provenance_forget_graph() -> None:
    c = _client()
    c.post("/v1/add", json={"text": "Hi, my name is Dana. I live in Delhi.",
                            "subject_id": "u1", "valid_from": "2019-01-01", "source_ref": "m1"})
    c.post("/v1/add", json={"text": "I moved to Berlin.",
                            "subject_id": "u1", "valid_from": "2026-03-01", "source_ref": "m2"})

    now = c.get("/v1/search", params={"q": "where does the user live", "subject_id": "u1"}).json()
    assert now["results"][0]["fact"]["object"] == "Berlin"

    then = c.get("/v1/search", params={"q": "where did the user live", "subject_id": "u1",
                                       "as_of": "2020-01-01"}).json()
    assert then["results"][0]["fact"]["object"] == "Delhi"

    tl = c.get("/v1/timeline", params={"subject_id": "u1"}).json()["edges"]
    assert any(e["object"] == "Delhi" and not e["alive"] for e in tl)

    berlin = next(e for e in tl if e["object"] == "Berlin")
    prov = c.get(f"/v1/provenance/{berlin['fact_id']}").json()
    assert prov["snippet"] == "Berlin" and prov["source_ref"] == "m2"

    graph = c.get("/v1/graph", params={"subject_id": "u1"}).json()
    assert any(n["id"] == "Berlin" for n in graph["nodes"])

    assert c.get("/v1/audit/verify").json()["ok"] is True

    cert = c.post("/v1/forget/u1").json()
    assert cert["facts_deleted"] >= 2
    after = c.get("/v1/search", params={"q": "where does the user live", "subject_id": "u1"}).json()
    assert after["results"] == []


def test_console_is_served() -> None:
    r = _client().get("/")
    assert r.status_code == 200
    assert "reactflow" in r.text.lower()


def test_audit_verify_deep_catches_content_tamper() -> None:
    """?deep=true re-derives event digests: an in-place edit of a stored fact
    passes chain-only verification but fails deep verification at its seq."""
    import dataclasses

    from attestari.events import FactAsserted

    mem = Memory()
    c = TestClient(create_app(mem))
    c.post("/v1/add", json={"text": "I live in Berlin.", "subject_id": "u1"})

    assert c.get("/v1/audit/verify", params={"deep": "true"}).json()["ok"] is True

    # Tamper: silently rewrite the stored fact's object, leaving the chain alone.
    log = mem.store._log
    i, ev = next((i, e) for i, e in enumerate(log) if isinstance(e, FactAsserted))
    log[i] = dataclasses.replace(ev, object="Pyongyang")

    shallow = c.get("/v1/audit/verify").json()
    deep = c.get("/v1/audit/verify", params={"deep": "true"}).json()
    assert shallow["ok"] is True          # chain-only cannot see content edits
    assert deep["ok"] is False and deep["broken_at"] is not None
    assert deep["deep"] is True


def test_forget_returns_signature_fields() -> None:
    c = _client()
    c.post("/v1/add", json={"text": "I live in Berlin.", "subject_id": "u1"})
    cert = c.post("/v1/forget/u1").json()
    # NullCipher deployment: fields present, honestly null.
    assert "signature" in cert and "algorithm" in cert
    assert cert["signature"] is None and cert["algorithm"] is None
    assert cert["dry_run"] is False


def test_forget_dry_run_previews_without_destroying() -> None:
    c = _client()
    c.post("/v1/add", json={"text": "I live in Berlin.", "subject_id": "u1"})

    cert = c.post("/v1/forget/u1", params={"dry_run": "true"}).json()
    assert cert["dry_run"] is True          # marked as a preview
    assert cert["signature"] is None        # a preview is never signed
    assert cert["facts_deleted"] >= 1       # reports the real blast radius
    # Nothing destroyed — the subject is still recallable.
    still = c.get("/v1/search", params={"q": "where does the user live", "subject_id": "u1"}).json()
    assert still["results"] != []

    # A real forget afterwards is not a dry run and does erase.
    real = c.post("/v1/forget/u1").json()
    assert real["dry_run"] is False
    gone = c.get("/v1/search", params={"q": "where does the user live", "subject_id": "u1"}).json()
    assert gone["results"] == []


def test_malformed_dates_are_422_not_500() -> None:
    c = _client()
    r = c.get("/v1/search", params={"q": "x", "as_of": "not-a-date"})
    assert r.status_code == 422 and "as_of" in r.json()["detail"]
    r = c.post("/v1/add", json={"text": "I live in Berlin.", "valid_from": "13/01/2026"})
    assert r.status_code == 422 and "valid_from" in r.json()["detail"]
    assert c.get("/v1/search", params={"q": "x", "limit": 0}).status_code == 422
