# notari-langchain

LangChain integration for [Notari](https://github.com/notarihq/notari) — the
auditable memory layer for AI agents.

`NotariMemory` is a LangChain `BaseMemory` you can drop into any chain or agent.
Instead of replaying a raw chat transcript, it stores **facts with provenance**:

- **on load** — searches the subject's relevant facts and injects them into the prompt;
- **on save** — ingests the turn, so facts are extracted, deduped, and superseded;
- **`clear()`** — maps to Notari's provable deletion (`forget`).

You get bi-temporal recall, provenance, a tamper-evident audit trail, and a signed
deletion certificate — on plain Postgres, or zero-dependency in memory.

## Install

```bash
pip install notari-langchain        # pulls in notari + langchain-core
```

## Use

```python
from notari import Memory
from notari_langchain import NotariMemory
from langchain.chains import ConversationChain
from langchain_anthropic import ChatAnthropic

memory = NotariMemory(
    mem=Memory(),            # or Memory.postgres() for durable storage
    subject_id="user_42",    # an opaque pseudonym, not raw PII
    k=5,                     # facts to recall per turn
)

chain = ConversationChain(
    llm=ChatAnthropic(model="claude-opus-4-8"),
    memory=memory,
)

chain.predict(input="Hi, I'm Alice and I live in Toronto.")
chain.predict(input="Where do I live?")     # recalls "Alice lives in Toronto"

memory.clear()               # provable deletion for this subject
```

## Options

| Field | Default | Purpose |
|---|---|---|
| `mem` | — | The Notari engine: `Memory()` or `Memory.postgres()`. |
| `subject_id` | — | Whose memory this is. |
| `memory_key` | `"history"` | Prompt variable the recalled facts are injected as. |
| `k` | `5` | How many facts to recall per turn. |
| `learn_ai_messages` | `True` | Also ingest the model's replies, not just user input. |
| `input_key` / `output_key` | auto | Which fields are the message / reply (auto-detected if unambiguous). |

Apache-2.0.
