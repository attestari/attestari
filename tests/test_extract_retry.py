"""AnthropicExtractor retry/backoff + the eval's crash-safe extraction cache."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from attestari.events import Scope
from attestari.extract import AnthropicExtractor

anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")


def _status_error(status: int, headers: dict | None = None) -> anthropic.APIStatusError:
    response = httpx.Response(
        status,
        headers=headers or {},
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    cls = {429: anthropic.RateLimitError, 400: anthropic.BadRequestError}.get(
        status, anthropic.APIStatusError
    )
    return cls("boom", response=response, body=None)


class _FakeMessages:
    """Fails `failures` times with `error`, then succeeds."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def parse(self, **params):  # noqa: ANN003
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        fact = SimpleNamespace(subject="u1", predicate="lives_in", object="Berlin", confidence=1.0)
        return SimpleNamespace(parsed_output=SimpleNamespace(facts=[fact]))


def _client(failures: int, error: Exception):
    return SimpleNamespace(messages=_FakeMessages(failures, error))


def test_retries_through_rate_limits() -> None:
    # Two 429s (retry-after: 0 so the test is instant), then success.
    client = _client(2, _status_error(429, {"retry-after": "0"}))
    ex = AnthropicExtractor(client=client, max_retries=3)
    facts = ex.extract("I live in Berlin", Scope(subject_id="u1"))
    assert client.messages.calls == 3
    assert facts and facts[0].object == "Berlin"


def test_retries_exhausted_raises() -> None:
    client = _client(99, _status_error(429, {"retry-after": "0"}))
    ex = AnthropicExtractor(client=client, max_retries=1)
    with pytest.raises(anthropic.RateLimitError):
        ex.extract("I live in Berlin", Scope(subject_id="u1"))
    assert client.messages.calls == 2  # initial attempt + 1 retry


def test_non_transient_error_not_retried() -> None:
    client = _client(99, _status_error(400))
    ex = AnthropicExtractor(client=client, max_retries=5)
    with pytest.raises(anthropic.BadRequestError):
        ex.extract("I live in Berlin", Scope(subject_id="u1"))
    assert client.messages.calls == 1  # auth/invalid-request errors fail fast


# --- default_extractor: env-driven production upgrade ----------------------- #

def test_default_extractor_regex_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from attestari.extract import DeterministicExtractor, default_extractor

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(default_extractor(), DeterministicExtractor)


def test_default_extractor_claude_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from attestari.extract import default_extractor

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ATTESTARI_EXTRACTOR_MODEL", "claude-haiku-4-5")
    ex = default_extractor()
    assert isinstance(ex, AnthropicExtractor)
    assert ex.model == "claude-haiku-4-5"  # env override respected
    monkeypatch.delenv("ATTESTARI_EXTRACTOR_MODEL")
    assert default_extractor().model == "claude-opus-4-8"  # class default


def test_default_extractor_falls_back_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    from attestari.extract import DeterministicExtractor, default_extractor

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    assert isinstance(default_extractor(), DeterministicExtractor)


# --- eval extraction: checkpoint + resume ----------------------------------- #

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root for `eval`


def _sample(n_turns: int) -> dict:
    return {
        "conversation": {
            "session_1_date_time": "1:00 pm on 1 January, 2023",
            "session_1": [
                {"speaker": "u1", "dia_id": f"D1:{i}", "text": f"turn {i}"}
                for i in range(1, n_turns + 1)
            ],
        }
    }


def test_extraction_checkpoints_and_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import eval.locomo as locomo

    calls = {"n": 0, "fail_at": 3}

    class FlakyExtractor:
        def __init__(self, model: str) -> None:  # matches AnthropicExtractor(model=...)
            pass

        def extract(self, text: str, scope: Scope) -> list:
            calls["n"] += 1
            if calls["n"] == calls["fail_at"]:
                calls["fail_at"] = -1  # fail exactly once (a stray 429 mid-run)
                raise RuntimeError("simulated rate-limit death")
            return []

    monkeypatch.setattr(locomo, "AnthropicExtractor", FlakyExtractor)
    cache = tmp_path / "facts.json"
    args = Namespace(cache=str(cache), sample=0, max_sessions=8, turn_cap=4,
                     model="fake", sleep=0.0)

    # First run dies at turn 3 — but turns 1-2 are checkpointed to the partial.
    with pytest.raises(RuntimeError):
        locomo.load_or_extract(_sample(4), args)
    partial = cache.with_name(cache.name + ".partial")
    assert not cache.exists()
    assert len(json.loads(partial.read_text())) == 2

    # Second run resumes from the partial: only turns 3-4 hit the "API".
    before = calls["n"]
    turns = locomo.load_or_extract(_sample(4), args)
    assert len(turns) == 4
    assert calls["n"] - before == 2  # no re-extraction of checkpointed turns
    assert cache.exists() and not partial.exists()
    assert len(json.loads(cache.read_text())) == 4
