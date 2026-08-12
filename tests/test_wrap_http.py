"""Tests for the HTTP form of wrap — governing a memory service over the wire.

These run a real `http.server` on a loopback port rather than mocking urllib:
the point of this module is the wire format, and a mock would happily validate
a request shape the client never actually sends.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from attestari import Memory
from attestari.wrap import wrap
from attestari.wrap_http import HTTPMemoryClient, UpstreamError, http_adapter


class _Upstream(BaseHTTPRequestHandler):
    """A minimal memory service speaking the documented contract."""

    store: dict[str, list[str]] = {}
    requests: list[tuple[str, dict]] = []
    lying_delete = False
    fail_add = False

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or "{}")
        type(self).requests.append((self.path, body))
        subject = body.get("subject_id")

        if self.path == "/add":
            if type(self).fail_add:
                return self._send(503, {"error": "overloaded"})
            type(self).store.setdefault(subject, []).append(body["text"])
            return self._send(200, {"stored": True})
        if self.path == "/search":
            return self._send(200, {"results": type(self).store.get(subject, [])})
        if self.path == "/delete":
            if not type(self).lying_delete:
                type(self).store.pop(subject, None)
            return self._send(200, {"deleted": True})
        if self.path == "/get_all":
            return self._send(200, {"results": type(self).store.get(subject, [])})
        return self._send(404, {"error": "no such path"})

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # keep the test output clean
        pass


@pytest.fixture
def upstream():
    _Upstream.store, _Upstream.requests = {}, []
    _Upstream.lying_delete = _Upstream.fail_add = False
    server = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _Upstream
    server.shutdown()


def _governed(base_url: str, **kw):
    client = HTTPMemoryClient(base_url, **kw)
    return wrap(
        client,
        ledger=Memory(),
        adapter=http_adapter(verify=client.get_all_path is not None),
    )


def test_write_read_and_erase_over_http(upstream) -> None:
    base_url, service = upstream
    governed = _governed(base_url)

    governed.add("I live in Delhi.", subject_id="u1")
    assert service.store["u1"] == ["I live in Delhi."]
    assert governed.search("where do I live", subject_id="u1") == {"results": ["I live in Delhi."]}

    receipt = governed.forget("u1", requested_by="dpo@example.com")

    assert receipt.complete is True
    assert receipt.downstream_verified is True   # read back over HTTP, and empty
    assert service.store == {}
    assert governed.is_forgotten("u1")
    assert governed.verify_audit(deep=True).ok
    # The wire really carried the documented contract.
    assert [path for path, _ in service.requests] == ["/add", "/search", "/delete", "/get_all"]


def test_a_service_that_claims_deletion_but_keeps_the_data_is_caught(upstream) -> None:
    base_url, service = upstream
    service.lying_delete = True
    governed = _governed(base_url)
    governed.add("I live in Delhi.", subject_id="u1")

    receipt = governed.forget("u1")

    assert receipt.downstream_ok is False
    assert receipt.downstream_verified is False
    assert receipt.complete is False
    assert "remains after deletion" in receipt.downstream_error


def test_without_a_get_all_endpoint_the_result_is_unverified(upstream) -> None:
    base_url, _ = upstream
    governed = _governed(base_url, get_all_path=None)
    governed.add("I live in Delhi.", subject_id="u1")

    receipt = governed.forget("u1")

    assert receipt.downstream_ok is True
    assert receipt.downstream_verified is None  # not True — nobody checked


def test_upstream_http_error_propagates_with_the_attempt_recorded(upstream) -> None:
    base_url, service = upstream
    service.fail_add = True
    governed = _governed(base_url)

    with pytest.raises(UpstreamError, match="HTTP 503"):
        governed.add("I live in Delhi.", subject_id="u1")

    # The ledger recorded the attempt before the upstream was called, so a
    # failed write is still visible rather than vanishing.
    assert governed.timeline(subject_id="u1")
    assert governed.verify_audit(deep=True).ok


def test_unreachable_upstream_is_a_failed_deletion_not_a_silent_pass() -> None:
    governed = _governed("http://127.0.0.1:9")  # discard port: nothing listens
    receipt = governed.forget("u1")

    assert receipt.downstream_ok is False
    assert receipt.downstream_verified is None  # never reached the check
    assert receipt.complete is False
    assert receipt.downstream_error is not None
    # Our own copy is still shredded, and the chain records the failure.
    assert governed.is_forgotten("u1")
    assert governed.verify_audit(deep=True).ok


# --- the REST surface: governance for non-Python callers ------------------- #


def _app_client(monkeypatch, base_url: str, **env):
    """A TestClient whose server has ATTESTARI_WRAP_UPSTREAM configured."""
    from fastapi.testclient import TestClient

    from attestari.server import create_app

    monkeypatch.setenv("ATTESTARI_WRAP_UPSTREAM", base_url)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return TestClient(create_app(Memory()))


def test_rest_wrap_endpoints_govern_the_upstream(monkeypatch, upstream) -> None:
    base_url, service = upstream
    client = _app_client(monkeypatch, base_url)

    assert client.post("/v1/wrap/add", json={"text": "I live in Delhi.", "subject_id": "u1"}).status_code == 200
    assert service.store["u1"] == ["I live in Delhi."]

    search = client.post("/v1/wrap/search", json={"query": "where", "subject_id": "u1"})
    assert search.json()["upstream"] == {"results": ["I live in Delhi."]}

    forget = client.post("/v1/wrap/forget/u1", params={"requested_by": "dpo@example.com"})
    body = forget.json()
    assert forget.status_code == 200
    assert body["complete"] is True
    assert body["downstream"]["verified"] is True
    assert body["certificate"]["subject_id"] == "u1"
    assert service.store == {}


def test_rest_partial_erasure_returns_409_not_200(monkeypatch, upstream) -> None:
    """A caller reading only the status code must not mistake a half-completed
    erasure for a successful one."""
    base_url, service = upstream
    service.lying_delete = True
    client = _app_client(monkeypatch, base_url)
    client.post("/v1/wrap/add", json={"text": "I live in Delhi.", "subject_id": "u1"})

    response = client.post("/v1/wrap/forget/u1")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["complete"] is False
    assert detail["downstream"]["verified"] is False


def test_wrap_routes_are_absent_when_no_upstream_is_configured(monkeypatch) -> None:
    """Routes that would 500 on every call are worse than no routes; their
    absence is the signal that nothing is being governed."""
    from fastapi.testclient import TestClient

    from attestari.server import create_app

    monkeypatch.delenv("ATTESTARI_WRAP_UPSTREAM", raising=False)
    client = TestClient(create_app(Memory()))

    assert client.post("/v1/wrap/add", json={"text": "x", "subject_id": "u1"}).status_code == 404
    assert "/v1/wrap/add" not in client.get("/openapi.json").json()["paths"]
