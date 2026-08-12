"""Wrap a memory service that lives behind HTTP.

`wrap()` governs a Python object. When the memory layer is a *service* — your
own, or one written in another language — this gives that service the same
object shape, so nothing about `wrap` needs to change:

    from attestari.wrap import wrap
    from attestari.wrap_http import HTTPMemoryClient, http_adapter

    upstream = HTTPMemoryClient("https://memory.internal", headers={...})
    governed = wrap(upstream, adapter=http_adapter())

**Scope.** This is for services you control (or that speak a small, declared
contract), not a universal proxy for any vendor's REST API. Field and path names
are configurable, but a hosted API with a genuinely different request shape is
better wrapped through its own SDK and the Python `Adapter` — that path is
exact, and guessing at someone else's wire format is how a deletion silently
targets nothing.

Uses stdlib `urllib` only: the governance path must not drag a new dependency
into a package whose core is dependency-free.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .wrap import Adapter


class UpstreamError(RuntimeError):
    """The upstream refused or failed. Raised so `wrap.forget()` records it as
    a failed downstream deletion rather than reporting a success that didn't
    happen."""


@dataclass
class HTTPMemoryClient:
    """A memory service reached over HTTP, shaped like the clients `wrap` knows.

    The default contract is deliberately small — POST JSON, get JSON back:

        POST {base_url}/add      {"text": …,  "subject_id": …}
        POST {base_url}/search   {"query": …, "subject_id": …}
        POST {base_url}/delete   {"subject_id": …}
        POST {base_url}/get_all  {"subject_id": …}   (optional; enables read-back)

    Set `get_all_path=None` if the service can't list what it holds. `forget()`
    then reports `downstream_verified=None` — unverified, which is honest, and
    never `True`.
    """

    base_url: str
    add_path: str = "/add"
    search_path: str = "/search"
    delete_path: str = "/delete"
    get_all_path: str | None = "/get_all"
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 10.0
    text_field: str = "text"
    query_field: str = "query"
    subject_field: str = "subject_id"

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **self.headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:  # a 4xx/5xx is a real failure, not a result
            raise UpstreamError(f"{path} -> HTTP {exc.code}: {exc.read()[:200]!r}") from exc
        except urllib.error.URLError as exc:
            raise UpstreamError(f"{path} -> {exc.reason}") from exc
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            # Not JSON: hand back the raw text rather than guessing. For a
            # read-back this counts as "something is still there" (see
            # wrap._is_nonempty), which is the safe way to be wrong.
            return body.decode(errors="replace")

    def add(self, text: str, **kw: Any) -> Any:
        return self._post(
            self.add_path, {self.text_field: text, self.subject_field: kw[self.subject_field]}
        )

    def search(self, query: str, **kw: Any) -> Any:
        return self._post(
            self.search_path, {self.query_field: query, self.subject_field: kw[self.subject_field]}
        )

    def delete(self, **kw: Any) -> Any:
        return self._post(self.delete_path, {self.subject_field: kw[self.subject_field]})

    def get_all(self, **kw: Any) -> Any:
        if self.get_all_path is None:
            raise UpstreamError("this upstream has no get_all endpoint configured")
        return self._post(self.get_all_path, {self.subject_field: kw[self.subject_field]})


def http_adapter(*, verify: bool = True) -> Adapter:
    """Adapter for `HTTPMemoryClient`. `verify=False` drops the post-delete
    read-back for a service that can't list its contents."""
    return Adapter(
        add="add",
        search="search",
        delete="delete",
        subject_kwarg="subject_id",
        verify="get_all" if verify else None,
    )
