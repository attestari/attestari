"""LangChain integration for Notari — the auditable memory layer for AI agents.

Two pieces, both built on the stable `langchain-core` primitives:

- **`NotariRetriever`** (`BaseRetriever`) — recall a subject's relevant facts and
  hand them to a chain/agent as `Document`s, each carrying **provenance** in its
  metadata (fact id, source episode, valid-from, confidence). Supports bi-temporal
  `as_of` retrieval. This is the idiomatic way to inject long-term memory into a
  modern LCEL / RAG chain or an agent tool.
- **`NotariChatMessageHistory`** (`BaseChatMessageHistory`) — a drop-in history for
  `RunnableWithMessageHistory`: it *learns facts* from each turn (not a raw
  transcript), surfaces the subject's known facts as context, and maps `clear()`
  to Notari's provable deletion (`forget`).

    from notari import Memory
    from notari_langchain import NotariRetriever

    mem = Memory()                       # or Memory.postgres() for durable storage
    mem.add("Alice lives in Toronto.", subject_id="user_42")
    retriever = NotariRetriever(mem=mem, subject_id="user_42")
    retriever.invoke("where does the user live?")   # -> [Document("user lives_in Toronto", ...)]
"""

from __future__ import annotations

from typing import Any

from notari import Memory

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.documents import Document
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

__all__ = ["NotariRetriever", "NotariChatMessageHistory"]
__version__ = "0.0.1"


def _triple(edge: Any) -> str:
    return f"{edge.subject} {edge.predicate.replace('_', ' ')} {edge.object}"


class NotariRetriever(BaseRetriever):
    """Retrieve a subject's relevant facts as provenance-carrying `Document`s."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mem: Memory
    """The Notari engine — `Memory()` (in-memory) or `Memory.postgres()` (durable)."""
    subject_id: str | None = None
    """Scope retrieval to one subject. Use an opaque pseudonym, not raw PII."""
    k: int = 5
    """How many facts to recall."""
    as_of: str | None = None
    """Optional bi-temporal instant — recall what was true `as_of` this date."""

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        results = self.mem.search(
            query, subject_id=self.subject_id, as_of=self.as_of, limit=self.k
        )
        docs: list[Document] = []
        for r in results:
            e = r.edge
            docs.append(
                Document(
                    page_content=_triple(e),
                    metadata={
                        "fact_id": e.fact_id,
                        "subject": e.subject,
                        "predicate": e.predicate,
                        "object": e.object,
                        "valid_from": e.valid_from.isoformat() if e.valid_from else None,
                        "source_episode_id": e.source_episode_id,
                        "confidence": e.confidence,
                        "score": r.score,
                    },
                )
            )
        return docs


class NotariChatMessageHistory(BaseChatMessageHistory):
    """A `BaseChatMessageHistory` that learns facts instead of storing a transcript.

    Drop into `RunnableWithMessageHistory`. `add_messages` ingests each turn into
    Notari; `messages` returns the subject's current facts as a single system
    message; `clear` performs provable deletion.
    """

    def __init__(self, mem: Memory, subject_id: str):
        self.mem = mem
        self.subject_id = subject_id

    @property
    def messages(self) -> list[BaseMessage]:
        live = [
            e
            for e in self.mem.timeline(subject_id=self.subject_id)
            if e.valid_to is None
        ]
        if not live:
            return []
        facts = "\n".join(f"- {_triple(e)}" for e in live)
        return [SystemMessage(content=f"Known facts about the user:\n{facts}")]

    def add_messages(self, messages: list[BaseMessage]) -> None:
        for m in messages:
            content = m.content if isinstance(m.content, str) else str(m.content)
            if content.strip():
                self.mem.add(content, subject_id=self.subject_id)

    def clear(self) -> None:
        """Provable deletion: forget everything for this subject."""
        self.mem.forget(self.subject_id)
