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
    # Optional: read back whatever the store still holds for a subject. When
    # set, `forget()` calls it *after* the delete and only reports success if
    # nothing remains — see WrappedMemory.forget. Leaving it unset means a
    # deletion is trusted on the delete call's say-so, which is exactly the kind
    # of promise this project exists to replace with a check.
    verify: str | Callable[..., Any] | None = None

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

    def call_verify(self, client: Any, subject_id: str) -> Any:
        if self.verify is None:
            raise RuntimeError("adapter has no verify operation")
        return self._bind(client, self.verify, "verify")(**{self.subject_kwarg: subject_id})


def mem0_adapter() -> Adapter:
    """Mem0 2.x — both `mem0.Memory` (local) and `mem0.MemoryClient` (hosted).

    Verified against mem0ai 2.0.18. The asymmetry is theirs and is easy to get
    wrong: `add` and `delete_all` take the subject as a top-level `user_id`,
    but `search`/`get_all` require it **inside `filters`** — the local class
    actively rejects a top-level `user_id` there (`_reject_top_level_entity_params`),
    which is good of them: a silently unfiltered search would return other
    subjects' memories.
    """
    return Adapter(
        add="add",
        search=lambda client, query, **kw: client.search(
            query, filters={"user_id": kw["user_id"]}
        ),
        delete="delete_all",
        subject_kwarg="user_id",
        # Read back what survived the delete rather than trusting its return.
        verify=lambda client, **kw: client.get_all(filters={"user_id": kw["user_id"]}),
    )


def zep_adapter() -> Adapter:
    """Zep Cloud v3 (`zep_cloud.Zep`). Verified against zep-cloud 3.28.0.

    Nothing about this one fits the method-name shape, which is why `Adapter`
    takes callables: the operations live on **sub-clients** (`client.graph`,
    `client.user`), `add` wants `data=`/`type=` rather than a positional string,
    `search` is keyword-only, and deletion is `user.delete(user_id)` — a
    positional argument that removes the whole user.

    `verify` uses `user.get()`: after `user.delete()` the user should no longer
    resolve, so a call that still returns one means the erasure didn't take.
    Zep raises `NotFoundError` in that case, which `forget()` reads as "gone"
    — see `_zep_verify`.
    """
    return Adapter(
        add=lambda client, text, **kw: client.graph.add(
            data=text, type="text", user_id=kw["user_id"]
        ),
        search=lambda client, query, **kw: client.graph.search(
            query=query, user_id=kw["user_id"]
        ),
        delete=lambda client, **kw: client.user.delete(kw["user_id"]),
        subject_kwarg="user_id",
        verify=_zep_verify,
    )


def _zep_verify(client: Any, **kw: Any) -> Any:
    """Has the user really gone? Zep signals absence by raising, so translate
    that into "nothing remains" rather than letting it read as a failed check.
    Any other error is a genuinely unverifiable delete and must propagate."""
    try:
        return client.user.get(kw["user_id"])
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        if _is_not_found(exc):
            return None
        raise


def _is_not_found(exc: BaseException) -> bool:
    """Is this the client's "no such record" signal?

    Prefer the real exception class — anyone using `zep_adapter()` necessarily
    has zep installed — and fall back to the class name so a moved or renamed
    import degrades to a slightly looser match instead of turning a successful
    deletion into a reported failure.
    """
    try:
        from zep_cloud import NotFoundError  # type: ignore[import-not-found]

        if isinstance(exc, NotFoundError):
            return True
    except Exception:  # noqa: BLE001 - zep absent or restructured; use the fallback
        pass
    return type(exc).__name__ in {"NotFoundError", "NotFoundException"}


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """What a wrapped `forget()` returns: Attestari's own signed certificate,
    plus what the downstream store did when asked to delete.

    `complete` is True only when *both* halves succeeded. A downstream failure
    is reported, not swallowed — an erasure that only half happened is exactly
    the thing this system exists to make visible.

    `downstream_verified` distinguishes two very different situations that a
    bare success flag would blur: **True** means we read the store back after
    the delete and nothing remained; **None** means the delete call reported
    success and we took its word for it (no `verify` on the adapter). Only the
    first is evidence."""

    certificate: DeletionCertificate
    downstream_called: bool
    downstream_ok: bool
    downstream_result: Any = None
    downstream_error: str | None = None
    downstream_verified: bool | None = None
    downstream_remaining: Any = None

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
        verified: bool | None = None
        remaining: Any = None
        try:
            result = self.adapter.call_delete(self.client, subject_id)
            ok = True
        except Exception as exc:  # noqa: BLE001 - the failure is the evidence
            error = f"{type(exc).__name__}: {exc}"

        # Don't take the delete call's word for it. If the adapter can read the
        # subject back, do — a store that returns 200 and keeps the data is the
        # precise failure this product exists to catch, and it is invisible to
        # anyone who only checks the response code.
        if ok and self.adapter.verify is not None:
            try:
                remaining = self.adapter.call_verify(self.client, subject_id)
                verified = not _is_nonempty(remaining)
                if not verified:
                    ok = False
                    error = (
                        "downstream reported success but data for the subject "
                        "remains after deletion"
                    )
            except Exception as exc:  # noqa: BLE001 - an unverifiable delete is not a verified one
                verified = False
                ok = False
                error = f"post-delete verification failed — {type(exc).__name__}: {exc}"

        # Record the downstream outcome *before* shredding, under wrap's own
        # scope so it survives the subject's erasure (see WRAP_AGENT_ID). This
        # is what makes "we asked, and here is what came back" auditable rather
        # than merely asserted.
        checked = {True: "verified empty", False: "VERIFICATION FAILED", None: "unverified"}[
            verified
        ]
        self.ledger.add(
            f"downstream deletion for subject {subject_id}: "
            f"{'ok' if ok else 'FAILED'} ({checked})"
            f"{'' if error is None else ' — ' + error}",
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
            downstream_verified=verified,
            downstream_remaining=remaining,
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


def _is_nonempty(result: Any) -> bool:
    """Did a read-back turn up anything for the subject?

    Memory layers answer "what do you still hold" in incompatible shapes: a
    list, or a dict like `{"results": [...]}`, or `{"memories": [...]}`. Treat
    an unrecognised non-empty object as *data still present* — for a deletion
    check, guessing "probably empty" is the dangerous direction to be wrong in.
    """
    if result is None:
        return False
    if isinstance(result, dict):
        for key in ("results", "memories", "data", "items"):
            if key in result:
                return bool(result[key])
        return bool(result)
    if isinstance(result, (list, tuple, set)):
        return bool(result)
    return True


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
