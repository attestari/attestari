"""FastAPI server tests (in-memory engine via TestClient).

Skipped unless fastapi + httpx are installed (the `server`/`dev` extras).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from notari import Memory  # noqa: E402
from notari.server import create_app  # noqa: E402


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
