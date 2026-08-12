"""Tests for `attestari.wrap` — governing a memory layer you already run.

The fake client below stands in for Mem0/Zep so the suite stays dependency-free
and can force the case that matters most: a downstream delete that *fails*. The
guarantee under test isn't "deletion always works" — it's that whatever happened
is on the record and can't be quietly rewritten afterwards.
"""

from __future__ import annotations

import pytest

from attestari import Memory
from attestari.wrap import WRAP_AGENT_ID, Adapter, WrappedMemory, mem0_adapter, wrap


class FakeClient:
    """Mem0-shaped: add(text, user_id=…), search(query, user_id=…), delete_all(user_id=…)."""

    def __init__(self, *, fail_delete: bool = False):
        self.store: dict[str, list[str]] = {}
        self.searches: list[tuple[str, str | None]] = []
        self.fail_delete = fail_delete

    def add(self, text, user_id=None, **kw):
        self.store.setdefault(user_id, []).append(text)
        return {"id": f"mem-{len(self.store[user_id])}", "user_id": user_id}

    def search(self, query, user_id=None, **kw):
        self.searches.append((query, user_id))
        return [{"text": t} for t in self.store.get(user_id, [])]

    def delete_all(self, user_id=None, **kw):
        if self.fail_delete:
            raise RuntimeError("upstream 503")
        self.store.pop(user_id, None)
        return {"deleted": True, "user_id": user_id}


def _wrapped(**kw) -> tuple[WrappedMemory, FakeClient]:
    client = FakeClient(**kw)
    return wrap(client, ledger=Memory(), adapter=mem0_adapter()), client


def test_add_records_in_the_ledger_and_writes_downstream() -> None:
    governed, client = _wrapped()
    governed.add("Hi, I'm Dana. I live in Delhi.", subject_id="u1")

    # Downstream got the write...
    assert client.store["u1"] == ["Hi, I'm Dana. I live in Delhi."]
    # ...and Attestari independently recorded it, with the chain intact.
    assert governed.timeline(subject_id="u1")
    assert governed.verify_audit(deep=True).ok


def test_search_passes_straight_through() -> None:
    governed, client = _wrapped()
    governed.add("I live in Delhi.", subject_id="u1")

    results = governed.search("where do I live", subject_id="u1")
    assert results == [{"text": "I live in Delhi."}]
    assert client.searches == [("where do I live", "u1")]


def test_forget_deletes_both_sides_and_returns_a_complete_receipt() -> None:
    governed, client = _wrapped()
    governed.add("I live in Delhi.", subject_id="u1")
    governed.add("I live in Chennai.", subject_id="u2")

    receipt = governed.forget("u1", requested_by="dpo@example.com")

    assert receipt.complete is True
    assert receipt.downstream_ok is True
    assert receipt.certificate.subject_id == "u1"
    assert receipt.certificate.requested_by == "dpo@example.com"
    # Both stores dropped u1 and neither touched u2.
    assert "u1" not in client.store and client.store["u2"]
    assert governed.timeline(subject_id="u1") == []
    assert governed.is_forgotten("u1")
    assert governed.verify_audit(deep=True).ok


def test_downstream_failure_is_reported_not_swallowed() -> None:
    """The half-completed erasure is the case this system exists to expose."""
    governed, client = _wrapped(fail_delete=True)
    governed.add("I live in Delhi.", subject_id="u1")

    receipt = governed.forget("u1")

    assert receipt.downstream_called is True
    assert receipt.downstream_ok is False
    assert "upstream 503" in receipt.downstream_error
    assert receipt.complete is False          # ...even though our own side shredded
    assert governed.is_forgotten("u1")
    assert client.store["u1"]                 # downstream still holds it — visibly
    assert governed.verify_audit(deep=True).ok


def test_the_downstream_delete_record_survives_the_subject_erasure() -> None:
    """A record of "we asked them to delete X" scoped to X would be shredded
    along with X. It must outlive the erasure to be evidence of it."""
    ledger = Memory()
    governed = wrap(FakeClient(), ledger=ledger, adapter=mem0_adapter())
    governed.add("I live in Delhi.", subject_id="u1")
    governed.forget("u1")

    episodes = [
        e
        for e in ledger.store.events()
        if getattr(getattr(e, "scope", None), "agent_id", None) == WRAP_AGENT_ID
    ]
    assert len(episodes) == 1
    assert "u1" in episodes[0].payload and "ok" in episodes[0].payload
    # It carries the outcome and the subject id — and no subject content.
    assert "Delhi" not in episodes[0].payload


def test_dry_run_touches_neither_store() -> None:
    governed, client = _wrapped()
    governed.add("I live in Delhi.", subject_id="u1")

    receipt = governed.forget("u1", dry_run=True)

    assert receipt.certificate.dry_run is True
    assert receipt.certificate.signature is None  # a preview can't pass as proof
    assert receipt.downstream_called is False
    assert receipt.complete is False
    assert client.store["u1"] and governed.timeline(subject_id="u1")


def test_unknown_method_name_fails_loudly() -> None:
    """A wrong adapter must not silently leave calls ungoverned."""
    governed = wrap(FakeClient(), ledger=Memory(), adapter=Adapter(add="insert"))
    with pytest.raises(AttributeError, match="no callable 'insert'"):
        governed.add("hello", subject_id="u1")


def test_callable_adapter_supports_an_odd_client() -> None:
    """Clients that don't fit the method-name shape (positional subject, a
    different signature) are handled by passing callables."""

    class Odd:
        def __init__(self):
            self.rows: list[tuple[str, str]] = []

        def put(self, subject, body):
            self.rows.append((subject, body))

        def purge(self, subject):
            self.rows = [r for r in self.rows if r[0] != subject]
            return len(self.rows)

    odd = Odd()
    governed = wrap(
        odd,
        ledger=Memory(),
        adapter=Adapter(
            add=lambda c, text, **kw: c.put(kw["user_id"], text),
            delete=lambda c, **kw: c.purge(kw["user_id"]),
        ),
    )
    governed.add("I live in Delhi.", subject_id="u1")
    assert odd.rows == [("u1", "I live in Delhi.")]

    assert governed.forget("u1").complete is True
    assert odd.rows == []


def test_passthrough_reaches_vendor_specific_methods() -> None:
    governed, client = _wrapped()
    governed.add("I live in Delhi.", subject_id="u1")
    # `store` is the fake's own attribute, not part of the governed surface.
    assert governed.store == client.store
