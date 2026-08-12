"""Tests for `attestari.wrap` — governing a memory layer you already run.

The fake client below stands in for Mem0/Zep so the suite stays dependency-free
and can force the case that matters most: a downstream delete that *fails*. The
guarantee under test isn't "deletion always works" — it's that whatever happened
is on the record and can't be quietly rewritten afterwards.
"""

from __future__ import annotations

import importlib.util

import pytest

from attestari import Memory
from attestari.wrap import WRAP_AGENT_ID, Adapter, WrappedMemory, mem0_adapter, wrap


class FakeClient:
    """Mem0 2.x-shaped: `user_id` on add/delete_all, `filters` on search/get_all.

    That asymmetry is real (verified against mem0ai 2.0.18) and is what
    `mem0_adapter()` exists to absorb, so the fake mirrors it rather than a
    tidier API we wish they had.

    `lying_delete` models the failure mode that matters most: a store that
    reports success and keeps the data.
    """

    def __init__(self, *, fail_delete: bool = False, lying_delete: bool = False):
        self.store: dict[str, list[str]] = {}
        self.searches: list[tuple[str, str | None]] = []
        self.fail_delete = fail_delete
        self.lying_delete = lying_delete

    def add(self, text, user_id=None, **kw):
        self.store.setdefault(user_id, []).append(text)
        return {"id": f"mem-{len(self.store[user_id])}", "user_id": user_id}

    def search(self, query, filters=None, **kw):
        user_id = (filters or {}).get("user_id")
        self.searches.append((query, user_id))
        return [{"text": t} for t in self.store.get(user_id, [])]

    def get_all(self, filters=None, **kw):
        return {"results": list(self.store.get((filters or {}).get("user_id"), []))}

    def delete_all(self, user_id=None, **kw):
        if self.fail_delete:
            raise RuntimeError("upstream 503")
        if not self.lying_delete:
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
    assert receipt.downstream_verified is True  # read back, not merely reported
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


def test_a_store_that_reports_success_but_keeps_the_data_is_caught() -> None:
    """The failure a response code can't reveal, and the reason `verify` exists:
    delete_all returns {"deleted": True} while the rows are still there."""
    governed, client = _wrapped(lying_delete=True)
    governed.add("I live in Delhi.", subject_id="u1")

    receipt = governed.forget("u1")

    assert receipt.downstream_result == {"deleted": True, "user_id": "u1"}  # it claimed success
    assert receipt.downstream_verified is False                            # we checked
    assert receipt.downstream_ok is False
    assert receipt.complete is False
    assert "remains after deletion" in receipt.downstream_error
    assert receipt.downstream_remaining == {"results": ["I live in Delhi."]}
    assert governed.verify_audit(deep=True).ok


def test_without_a_verify_operation_the_result_is_unverified_not_verified() -> None:
    """No read-back capability must never be reported as a successful check."""
    client = FakeClient(lying_delete=True)
    governed = wrap(client, ledger=Memory(), adapter=Adapter(verify=None))
    governed.add("I live in Delhi.", subject_id="u1")

    receipt = governed.forget("u1")

    assert receipt.downstream_ok is True        # we only have its word
    assert receipt.downstream_verified is None  # ...and we say so, rather than claiming True
    assert client.store["u1"]                   # the data is, in fact, still there


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


@pytest.mark.skipif(
    importlib.util.find_spec("mem0") is None,
    reason="mem0 not installed (pip install mem0ai) — contract check only runs when it is",
)
def test_mem0_adapter_matches_the_real_mem0_signatures() -> None:
    """Bind our calls against mem0's own classes, so their next API change fails
    here instead of in someone's deletion path.

    This is the check a hand-written fake cannot do: mem0 2.x takes `user_id`
    on add/delete_all but requires it inside `filters` on search/get_all, and
    getting that wrong is how you end up reporting a deletion that never
    filtered to the right subject.
    """
    import inspect

    from mem0 import Memory as Mem0Local
    from mem0 import MemoryClient as Mem0Hosted

    calls = [
        ("add", ("text",), {"user_id": "u1"}),
        ("search", ("q",), {"filters": {"user_id": "u1"}}),
        ("delete_all", (), {"user_id": "u1"}),
        ("get_all", (), {"filters": {"user_id": "u1"}}),
    ]
    for cls in (Mem0Local, Mem0Hosted):
        for name, args, kwargs in calls:
            inspect.signature(getattr(cls, name)).bind(None, *args, **kwargs)
