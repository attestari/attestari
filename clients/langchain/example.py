#!/usr/bin/env python3
"""NotariRetriever + NotariChatMessageHistory — no LLM or API key needed.

    pip install langchain-core
    python clients/langchain/example.py
"""

from __future__ import annotations

import pathlib
import sys

# Make `notari` and `notari_langchain` importable without installing.
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from notari import Memory  # noqa: E402
from notari_langchain import NotariChatMessageHistory, NotariRetriever  # noqa: E402


def main() -> int:
    mem = Memory()
    history = NotariChatMessageHistory(mem, subject_id="user_42")

    # What RunnableWithMessageHistory would feed in as the conversation happens:
    from langchain_core.messages import AIMessage, HumanMessage

    history.add_messages([HumanMessage("Hi, I'm Alice and I live in Toronto.")])
    history.add_messages([AIMessage("Nice to meet you, Alice!")])
    history.add_messages([HumanMessage("I moved to Berlin.")])

    print("Memory injected into the prompt (history.messages):")
    print("  " + history.messages[0].content.replace("\n", "\n  "))

    # The retriever is how you inject relevant facts into a RAG/agent chain —
    # each Document carries provenance you can show or audit.
    retriever = NotariRetriever(mem=mem, subject_id="user_42", k=3)
    docs = retriever.invoke("where does the user live?")
    print("\nNotariRetriever results (with provenance):")
    for d in docs:
        print(f"  - {d.page_content!r}  [fact_id={d.metadata['fact_id'][:8]}…, "
              f"score={d.metadata['score']:.2f}]")

    # The differentiator: provable deletion via clear().
    history.clear()
    after = retriever.invoke("where does the user live?")

    assert any("Berlin" in d.page_content for d in docs), "expected Berlin to supersede Toronto"
    assert after == [], "expected nothing recalled after clear()"
    print("\n✅ Recall, supersession (Berlin > Toronto), and provable deletion all hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
