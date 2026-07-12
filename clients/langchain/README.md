# attestari-langchain

LangChain integration for [Attestari](https://github.com/attestari/attestari) — the
auditable memory layer for AI agents.

Two pieces, both built on stable `langchain-core` primitives, so instead of
replaying a raw chat transcript your chain stores **facts with provenance** —
bi-temporal recall, a tamper-evident audit trail, and a signed deletion
certificate, on plain Postgres or zero-dependency in memory:

- **`AttestariRetriever`** (`BaseRetriever`) — recall a subject's relevant facts as
  `Document`s, each carrying provenance in its metadata (fact id, source episode,
  valid-from, confidence). Supports bi-temporal `as_of` recall. The idiomatic way
  to inject long-term memory into an LCEL / RAG chain or an agent tool.
- **`AttestariChatMessageHistory`** (`BaseChatMessageHistory`) — a drop-in history for
  `RunnableWithMessageHistory`: it *learns facts* from each **human** turn (not a raw
  transcript, and never from the assistant's own words), surfaces the subject's known facts as a system message, and maps
  `clear()` to Attestari's provable deletion (`forget`).

## Install

```bash
pip install attestari-langchain        # pulls in attestari + langchain-core
```

## Use — inject memory into a chain (AttestariRetriever)

```python
from attestari import Memory
from attestari_langchain import AttestariRetriever

mem = Memory()                       # or Memory.local() / Memory.postgres()
mem.add("Hi, I'm Alice. I live in Toronto.", subject_id="user_42")

retriever = AttestariRetriever(
    mem=mem,                         # the Attestari engine
    subject_id="user_42",            # an opaque pseudonym, not raw PII
    k=5,                             # facts to recall
    # as_of="2022-01-01",            # optional: recall what was true then
)

docs = retriever.invoke("where does the user live?")
# -> [Document("user_42 lives in Toronto", metadata={fact_id, source_episode_id, score, ...})]
```

> The zero-dependency default extractor understands **first-person** statements
> ("I live in…", "my name is…"). For arbitrary third-person text, install
> `attestari[anthropic]` and set `ANTHROPIC_API_KEY` for Claude-powered extraction.

Drop `retriever` into any LCEL chain the way you would any other retriever; each
returned `Document` carries provenance in `.metadata` you can show or audit.

## Use — conversational memory (AttestariChatMessageHistory)

```python
from attestari import Memory
from attestari_langchain import AttestariChatMessageHistory
from langchain_core.messages import HumanMessage

history = AttestariChatMessageHistory(mem=Memory(), subject_id="user_42")
history.add_messages([HumanMessage("Hi, I'm Alice and I live in Toronto.")])
history.messages          # -> [SystemMessage("Known facts about the user:\n- user lives_in Toronto")]
history.clear()           # provable deletion (forget) for this subject
```

Wire it into `RunnableWithMessageHistory` as the `get_session_history` factory:

```python
from langchain_core.runnables.history import RunnableWithMessageHistory

chain_with_memory = RunnableWithMessageHistory(
    chain,
    lambda session_id: AttestariChatMessageHistory(mem=Memory.local(), subject_id=session_id),
    input_messages_key="input",
    history_messages_key="history",
)
```

## Constructor reference

**`AttestariRetriever`**

| Field | Default | Purpose |
|---|---|---|
| `mem` | — | The Attestari engine: `Memory()`, `Memory.local()`, or `Memory.postgres()`. |
| `subject_id` | `None` | Scope recall to one subject (opaque pseudonym). |
| `k` | `5` | How many facts to recall. |
| `as_of` | `None` | Bi-temporal instant — recall what was true `as_of` this ISO date. |

**`AttestariChatMessageHistory(mem, subject_id)`** — `add_messages(msgs)` ingests each
human turn (assistant messages are conversation, not testimony — they are not
attributed to the user as facts), `messages` returns the subject's live facts as one system message, and
`clear()` maps to provable deletion.

A runnable, no-LLM example is in [`example.py`](example.py):
`python clients/langchain/example.py`.

Apache-2.0.
