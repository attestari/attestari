"""Govern a memory layer you already run.

`wrap()` puts Attestari *in front of* an existing memory client (Mem0, Zep, a
bare vector store) instead of replacing it. Writes are recorded in the
tamper-evident chain with provenance before being passed downstream; reads pass
straight through — the wrapped service keeps doing what it is good at; and
`forget()` deletes downstream, crypto-shreds Attestari's own copy, and records
the whole exchange as evidence.

**The boundary, stated plainly.** Attestari cannot cryptographically shred data
held inside someone else's service — it does not hold their keys. What a wrapped
deployment proves is narrower and worth saying exactly:

1. the deletion was requested, at a recorded time, by a recorded requester;
2. the downstream delete was called, and what it returned (including failure);
3. Attestari's own copy of that subject is unrecoverable, with a signed
   certificate; and
4. none of the above was altered afterwards — it is all in the audit chain.

That is an *auditable deletion record across both systems*, not "crypto-shred
everywhere". A wrapped store is as erasable as its own delete endpoint is
honest. Wrapping makes that endpoint's behaviour evidence rather than a promise;
for full cryptographic erasure the data has to live in Attestari itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .records import DeletionCertificate

# The scope used for wrap's own bookkeeping episodes. It is deliberately *not*
# the subject's scope: a record of "we asked the downstream store to delete
# subject X" must survive X's erasure, and anything scoped to X is shredded by
# it. Only the subject id and the call's outcome are recorded here — never the
# subject's content.
WRAP_AGENT_ID = "_attestari_wrap"


class MemoryClient(Protocol):
    """Structural type for the thing being wrapped: any object at all. The
    adapter, not this protocol, knows how to call it."""


@dataclass(frozen=True, slots=True)
class Adapter:
    """How to drive one memory client.

    Memory layers disagree about method names and about how the subject is
    passed (`user_id`, `session_id`, positional…), so the mapping is data rather
    than a subclass per vendor. `delete` is expected to erase everything for the
    subject; if a client can only delete by record id, pass a `delete` callable
    that does the fan-out.
    """

    add: str | Callable[..., Any] = "add"
    search: str | Callable[..., Any] = "search"
    delete: str | Callable[..., Any] = "delete_all"
    subject_kwarg: str = "user_id"

    def _bind(self, client: Any, op: str | Callable[..., Any], label: str) -> Callable[..., Any]:
        if callable(op):
            return lambda *a, **k: op(client, *a, **k)
        fn = getattr(client, op, None)
        if not callable(fn):
            raise AttributeError(
                f"wrapped client {type(client).__name__!r} has no callable {op!r} "
                f"for the {label} operation — pass an Adapter with the right "
                f"method name, or a callable taking (client, ...)."
            )
        return fn

    def call_add(self, client: Any, text: str, subject_id: str, **kw: Any) -> Any:
        return self._bind(client, self.add, "add")(text, **{self.subject_kwarg: subject_id}, **kw)

    def call_search(self, client: Any, query: str, subject_id: str | None, **kw: Any) -> Any:
        fn = self._bind(client, self.search, "search")
        if subject_id is None:
            return fn(query, **kw)
        return fn(query, **{self.subject_kwarg: subject_id}, **kw)

    def call_delete(self, client: Any, subject_id: str, **kw: Any) -> Any:
        return self._bind(client, self.delete, "delete")(**{self.subject_kwarg: subject_id}, **kw)


def mem0_adapter() -> Adapter:
    """Mem0's surface: `add(messages, user_id=…)`, `search(query, user_id=…)`,
    `delete_all(user_id=…)`."""
    return Adapter(add="add", search="search", delete="delete_all", subject_kwarg="user_id")


def zep_adapter() -> Adapter:
    """Zep-style surface keyed on `session_id`. Verify against your client
    version — Zep's API has moved more than Mem0's."""
    return Adapter(add="add", search="search", delete="delete", subject_kwarg="session_id")


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """What a wrapped `forget()` returns: Attestari's own signed certificate,
    plus what the downstream store did when asked to delete.

    `complete` is True only when *both* halves succeeded. A downstream failure
    is reported, not swallowed — an erasure that only half happened is exactly
    the thing this system exists to make visible."""

    certificate: DeletionCertificate
    downstream_called: bool
    downstream_ok: bool
    downstream_result: Any = None
    downstream_error: str | None = None

    @property
    def complete(self) -> bool:
        return self.downstream_ok and not self.certificate.dry_run


class WrappedMemory:
    """A memory client with Attestari's guarantees recorded around it.

    Unknown attributes fall through to the wrapped client, so this stays a
    drop-in: anything vendor-specific you already call keeps working. Note that
    calls made through that passthrough are **not** recorded — only the
    operations below are governed.
    """

    def __init__(self, client: Any, ledger: Any, adapter: Adapter, *, requested_by: str = "system"):
        self.client = client
        self.ledger = ledger
        self.adapter = adapter
        self.requested_by = requested_by

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - trivial passthrough
        return getattr(self.client, name)

    # --- write ---------------------------------------------------------- #

    def add(self, text: str, *, subject_id: str, source_ref: str | None = None, **kw: Any) -> Any:
        """Record the write in the audit chain, then pass it downstream.

        Ledger first, deliberately: if the downstream write fails we want the
        attempt on record. The downstream exception still propagates — this
        governs the call, it does not swallow it."""
        self.ledger.add(text, subject_id=subject_id, source_ref=source_ref)
        return self.adapter.call_add(self.client, text, subject_id, **kw)

    # --- read ----------------------------------------------------------- #

    def search(self, query: str, *, subject_id: str | None = None, **kw: Any) -> Any:
        """Straight passthrough — the wrapped store's retrieval is why you kept
        it. Attestari's own view is available via `timeline()`."""
        return self.adapter.call_search(self.client, query, subject_id, **kw)

    # --- govern --------------------------------------------------------- #

    def forget(
        self, subject_id: str, *, requested_by: str | None = None, dry_run: bool = False
    ) -> DeletionReceipt:
        """Erase a subject from both stores and return the evidence.

        Order matters: downstream first, then the local shred. If downstream
        fails we still have a live local copy to reconcile against, whereas
        shredding first would leave us unable to say what should have been
        deleted. With `dry_run=True` nothing is touched in either store.
        """
        who = requested_by or self.requested_by

        if dry_run:
            return DeletionReceipt(
                certificate=self.ledger.forget(subject_id, requested_by=who, dry_run=True),
                downstream_called=False,
                downstream_ok=False,
            )

        called, ok, result, error = True, False, None, None
        try:
            result = self.adapter.call_delete(self.client, subject_id)
            ok = True
        except Exception as exc:  # noqa: BLE001 - the failure is the evidence
            error = f"{type(exc).__name__}: {exc}"

        # Record the downstream outcome *before* shredding, under wrap's own
        # scope so it survives the subject's erasure (see WRAP_AGENT_ID). This
        # is what makes "we asked, and here is what came back" auditable rather
        # than merely asserted.
        self.ledger.add(
            f"downstream deletion for subject {subject_id}: "
            f"{'ok' if ok else 'FAILED'}{'' if error is None else ' — ' + error}",
            agent_id=WRAP_AGENT_ID,
            source_ref=f"attestari-wrap:delete:{subject_id}",
        )

        certificate = self.ledger.forget(subject_id, requested_by=who)
        return DeletionReceipt(
            certificate=certificate,
            downstream_called=called,
            downstream_ok=ok,
            downstream_result=result,
            downstream_error=error,
        )

    # --- the governed view ---------------------------------------------- #

    def verify_audit(self, *, deep: bool = False) -> Any:
        return self.ledger.verify_audit(deep=deep)

    def timeline(self, *, subject_id: str) -> Any:
        return self.ledger.timeline(subject_id=subject_id)

    def get_provenance(self, fact_id: str) -> Any:
        return self.ledger.get_provenance(fact_id)

    def is_forgotten(self, subject_id: str) -> bool:
        return self.ledger.is_forgotten(subject_id)


def wrap(
    client: Any,
    *,
    ledger: Any = None,
    adapter: Adapter | None = None,
    requested_by: str = "system",
) -> WrappedMemory:
    """Put Attestari in front of an existing memory client.

        from attestari.wrap import wrap
        governed = wrap(mem0_client)

        governed.add("I live in Berlin.", subject_id="u1")   # recorded, then stored
        governed.search("where do I live", subject_id="u1")  # straight through
        receipt = governed.forget("u1")                      # both stores + proof

    `ledger` defaults to `Memory.local()` — the durable single-file store, since
    a governance record that dies with the process governs nothing. Pass
    `Memory.postgres()` for a shared service. `adapter` is inferred from the
    client's module when it looks like a known vendor, and otherwise defaults to
    the common `add`/`search`/`delete_all` + `user_id` shape; pass an `Adapter`
    explicitly for anything else.
    """
    if ledger is None:
        from .memory import Memory

        ledger = Memory.local()
    if adapter is None:
        adapter = _infer_adapter(client)
    return WrappedMemory(client, ledger, adapter, requested_by=requested_by)


def _infer_adapter(client: Any) -> Adapter:
    """Guess the vendor from the client's module path. A wrong guess surfaces
    immediately as a clear AttributeError from `Adapter._bind`, not as silently
    ungoverned calls."""
    module = (type(client).__module__ or "").lower()
    if "mem0" in module:
        return mem0_adapter()
    if "zep" in module:
        return zep_adapter()
    return Adapter()
