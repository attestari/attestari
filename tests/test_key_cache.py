"""A subject's data key after a rollback, and a subject's records after forget().

Up to 0.0.7 KeyManager cached each data key (DEK) by subject and handed it out
on later writes without checking the keyring. Three ways that went wrong:

- A rolled-back write. The first write for a new subject mints a key and saves
  it inside the write transaction. If that transaction rolled back, the saved
  key went with it but the cache kept it, and the subject's next writes were
  encrypted under a key nothing stored. Nobody could read them, then or later.
- A write after forget() in the same process. The shred removed the subject's
  key, so the next write minted a new one, and every read then tried to decrypt
  the erased content with it: InvalidTag on every read of the log, for every
  subject, until someone deleted the new key by hand.
- A write after forget() in another worker, which still had the old key cached.
  The write was sealed under the destroyed key and silently lost.

The keyring is now read on every write, and a forgotten subject's records stay
closed: the stores refuse new content for it with ForgottenSubjectError.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from attestari import ForgottenSubjectError, InMemoryEventStore, Memory  # noqa: E402
from attestari.crypto import generate_kek  # noqa: E402
from attestari.extract import DeterministicExtractor  # noqa: E402

DSN = os.environ.get("ATTESTARI_DATABASE_URL")

TIERS = [
    "memory",
    "sqlite",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(
            not DSN, reason="set ATTESTARI_DATABASE_URL to run Postgres tests"
        ),
    ),
]


class _Rollback(Exception):
    pass


@pytest.fixture
def engine(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`engine()` opens an encrypting engine on the tier's storage. On SQLite
    and Postgres each call is a new connection with its own key memo, the way
    another worker or a restarted process sees the log."""
    monkeypatch.setenv("ATTESTARI_KEK", generate_kek())
    tier = request.param
    if tier == "memory":
        store = InMemoryEventStore()
        return lambda **kw: Memory(store=store, **kw)
    if tier == "sqlite":
        path = tmp_path / "log.db"
        return lambda **kw: Memory.local(path, **kw)
    from attestari import PostgresEventStore

    PostgresEventStore(DSN).truncate()
    return lambda **kw: Memory.postgres(DSN, **kw)


def _cities(mem: Memory, subject_id: str) -> list[str]:
    return sorted(
        e.object for e in mem.timeline(subject_id=subject_id) if e.predicate == "lives_in"
    )


def _still_readable(mem: Memory) -> None:
    assert _cities(mem, "bob") == ["Rome"]
    assert _cities(mem, "alice") == []
    mem.add("I live in Oslo.", subject_id="carol")
    assert _cities(mem, "carol") == ["Oslo"]
    assert mem.verify_audit(deep=True).ok


@pytest.mark.parametrize("engine", TIERS, indirect=True)
def test_a_rolled_back_key_is_not_reused(engine) -> None:
    mem = engine()
    # The new subject's first write mints its key, then the transaction rolls
    # back: a caller's own write_lock block that raises, a full disk, a dropped
    # connection.
    with pytest.raises(_Rollback):
        with mem.store.write_lock():
            mem.add("I live in Berlin.", subject_id="alice")
            raise _Rollback

    mem.add("I live in Paris.", subject_id="alice")

    assert _cities(mem, "alice") == ["Paris"]
    assert _cities(engine(), "alice") == ["Paris"]
    assert engine().verify_audit(deep=True).ok


@pytest.mark.parametrize("engine", TIERS, indirect=True)
def test_a_forgotten_subject_refuses_new_content(engine) -> None:
    mem = engine()
    mem.add("I live in Rome.", subject_id="bob")
    mem.add("I live in Berlin.", subject_id="alice")
    mem.forget("alice")
    chain = len(mem.store.audit_entries())

    with pytest.raises(ForgottenSubjectError) as refused:
        mem.add("I live in Paris.", subject_id="alice")

    assert refused.value.subject_id == "alice"
    assert len(mem.store.audit_entries()) == chain  # nothing was written
    _still_readable(mem)
    _still_readable(engine())
    mem.forget("alice")  # forgetting again is still allowed


@pytest.mark.parametrize("engine", TIERS, indirect=True)
def test_a_worker_with_the_old_key_cannot_write_after_another_forgets(engine) -> None:
    writer, eraser = engine(), engine()
    writer.add("I live in Rome.", subject_id="bob")
    writer.add("I live in Berlin.", subject_id="alice")  # the writer now holds alice's key
    eraser.forget("alice")

    with pytest.raises(ForgottenSubjectError):
        writer.add("I live in Paris.", subject_id="alice")

    _still_readable(engine())


class _ForgetsMidway:
    """Extracts, then lets another worker erase the subject before the facts are
    written, as when a forget() lands during a slow Claude extraction call."""

    def __init__(self, eraser: Memory) -> None:
        self._eraser = eraser
        self.certificate = None

    def extract(self, text, scope):
        facts = DeterministicExtractor().extract(text, scope)
        self.certificate = self._eraser.forget(scope.subject_id)
        return facts


@pytest.mark.parametrize("engine", TIERS, indirect=True)
def test_a_forget_during_extraction_erases_the_episode_and_refuses_the_facts(engine) -> None:
    engine().add("I live in Rome.", subject_id="bob")
    engine().add("I live in Berlin.", subject_id="alice")
    extractor = _ForgetsMidway(engine())
    writer = engine(extractor=extractor)

    with pytest.raises(ForgottenSubjectError):
        writer.add("I live in Paris.", subject_id="alice")

    # The forget counted the episode this add() wrote before extracting.
    assert extractor.certificate.episodes_deleted == 2
    _still_readable(engine())
