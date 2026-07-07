"""Extractor port + two adapters.

* DeterministicExtractor — regex rules, zero dependencies. Drives the spike, the
  tests, and CI so the engine runs with no API key. It only understands a few
  first-person patterns, which is plenty to exercise assertion, supersession,
  bi-temporal recall, and deletion.
* AnthropicExtractor — real fact extraction with Claude via structured outputs.
  This is the production path; it needs `pip install notari[anthropic]` and an
  ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .events import Scope


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    char_span: tuple[int, int] | None = None


@runtime_checkable
class Extractor(Protocol):
    def extract(self, text: str, scope: Scope) -> list[ExtractedFact]: ...


# --- Deterministic (zero-dependency) -------------------------------------- #

# (predicate, trigger). The trigger is matched case-insensitively via a scoped
# inline flag `(?i:...)`, but the captured object stays case-sensitive so it only
# grabs a capitalised proper noun, e.g. "I moved to Berlin" -> (subj, lives_in, Berlin).
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("name_is", re.compile(r"(?i:\b(?:my name is|i am|i'm))\s+([A-Z][\w]+)")),
    ("lives_in", re.compile(r"(?i:\b(?:i live in|i now live in|i moved to|i relocated to))\s+([A-Z][\w]+)")),
    ("works_at", re.compile(r"(?i:\b(?:i work at|i work for|i joined|i now work at))\s+([A-Z][\w]+)")),
    ("uses", re.compile(r"(?i:\b(?:i use|i switched to|i now use|i prefer))\s+([A-Z][\w]+)")),
]


class DeterministicExtractor:
    """Pulls a handful of first-person facts via regex. First-person statements
    are attributed to the scope's subject (the data subject)."""

    def extract(self, text: str, scope: Scope) -> list[ExtractedFact]:
        subject = scope.subject_id or "subject"
        facts: list[ExtractedFact] = []
        for predicate, pattern in _RULES:
            for m in pattern.finditer(text):
                facts.append(
                    ExtractedFact(
                        subject=subject,
                        predicate=predicate,
                        object=m.group(1),
                        confidence=1.0,
                        char_span=m.span(1),
                    )
                )
        return facts


# --- Anthropic (Claude-powered) ------------------------------------------- #

_SYSTEM = (
    "You extract durable facts from a message as (subject, predicate, object) "
    "triples. Use the given subject id as the subject for any first-person "
    "statement ('I', 'my', 'me'). Predicates are short snake_case relations such "
    "as name_is, lives_in, works_at, uses. Extract only stable facts worth "
    "remembering, not pleasantries. Return an empty list if there are none."
)


class AnthropicExtractor:
    """Production extractor: Claude with structured outputs.

    Defaults to the most capable model (`claude-opus-4-8`). For high-volume
    extraction you may pass a cheaper tier explicitly, e.g.
    `AnthropicExtractor(model="claude-haiku-4-5")`.

    Transient API failures (429 rate limits, 5xx/overloaded, connection drops)
    are retried with exponential backoff, honouring `retry-after` when the API
    sends one — an ingestion call must not drop a memory write because of a
    momentary limit, and a long extraction run must not die at turn 95.
    Non-transient errors (auth, invalid request) are raised immediately.
    """

    def __init__(
        self,
        model: str = "claude-opus-4-8",
        client: object | None = None,
        max_retries: int = 8,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self._client = client

    def _ensure_client(self) -> object:
        if self._client is None:
            import anthropic  # imported lazily so the core stays dependency-free

            self._client = anthropic.Anthropic()
        return self._client

    def _parse_with_retry(self, client, **params):  # noqa: ANN001, ANN202
        import random
        import time

        import anthropic

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                return client.messages.parse(**params)  # type: ignore[attr-defined]
            except anthropic.APIConnectionError:
                if attempt == self.max_retries:
                    raise
                wait = delay
            except anthropic.APIStatusError as err:
                # Retry rate limits (429), conflicts/timeouts (408/409), and
                # server-side failures (5xx incl. 529 overloaded); anything
                # else (400 invalid request, 401 auth, ...) is not transient.
                if err.status_code not in (408, 409, 429) and err.status_code < 500:
                    raise
                if attempt == self.max_retries:
                    raise
                headers = getattr(getattr(err, "response", None), "headers", None) or {}
                try:
                    wait = max(float(headers.get("retry-after")), 0.0)
                except (TypeError, ValueError):
                    wait = delay
            time.sleep(wait + random.uniform(0, 0.5))
            delay = min(delay * 2, 60.0)

    def extract(self, text: str, scope: Scope) -> list[ExtractedFact]:
        from pydantic import BaseModel  # ships with the anthropic SDK

        class _Fact(BaseModel):
            subject: str
            predicate: str
            object: str
            confidence: float = 1.0

        class _Facts(BaseModel):
            facts: list[_Fact]

        client = self._ensure_client()
        subject_id = scope.subject_id or "subject"
        # Structured outputs via messages.parse() — the validated, recommended
        # path. No temperature (removed on Opus 4.8); small bounded output.
        resp = self._parse_with_retry(
            client,
            model=self.model,
            max_tokens=2000,
            system=f"{_SYSTEM}\n\nThe subject id for first-person statements is: {subject_id}",
            messages=[{"role": "user", "content": text}],
            output_format=_Facts,
        )
        parsed = resp.parsed_output
        if not parsed:
            return []
        out: list[ExtractedFact] = []
        for f in parsed.facts:
            lo = text.find(f.object)
            span = (lo, lo + len(f.object)) if lo >= 0 else None
            out.append(
                ExtractedFact(
                    subject=f.subject,
                    predicate=f.predicate,
                    object=f.object,
                    confidence=f.confidence,
                    char_span=span,
                )
            )
        return out
