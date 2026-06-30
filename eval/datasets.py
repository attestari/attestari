"""Datasets for the eval harness.

A dataset is (episodes, questions):
  * episode  = text to ingest, with a subject and a valid_from date
  * question = a query + scope + optional `as_of`, with the expected answer

The built-in dataset is a deterministic smoke benchmark answerable by the core
engine. `load("locomo")` is a stub showing where the real long-horizon benchmarks
attach.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Episode:
    text: str
    subject_id: str
    valid_from: str


@dataclass(frozen=True, slots=True)
class Question:
    query: str
    subject_id: str
    expected: str
    as_of: str | None = None


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    episodes: list[Episode]
    questions: list[Question]


_BUILTIN = Dataset(
    name="builtin",
    episodes=[
        Episode("My name is Maya. I live in Pune and I work at Initech.", "user_a", "2018-06-01"),
        Episode("Update: I moved to Lisbon.", "user_a", "2026-01-15"),
        Episode("I'm Ravi. I live in Chennai. I use Postgres.", "user_b", "2021-03-01"),
    ],
    questions=[
        Question("where does the user live", "user_a", "Lisbon"),
        Question("where did the user live", "user_a", "Pune", as_of="2019-01-01"),
        Question("where does the user work", "user_a", "Initech"),
        Question("what is the user's name", "user_a", "Maya"),
        Question("where does the user live", "user_b", "Chennai"),
        Question("what does the user use", "user_b", "Postgres"),
    ],
)


def from_records(name: str, episodes: list[dict], questions: list[dict]) -> Dataset:
    return Dataset(
        name=name,
        episodes=[Episode(**e) for e in episodes],
        questions=[Question(**q) for q in questions],
    )


def load_file(path: str) -> Dataset:
    """Load any dataset given as JSON:
    {"name", "episodes":[{text,subject_id,valid_from}],
     "questions":[{query,subject_id,expected,as_of?}]}."""
    data = json.loads(Path(path).read_text())
    return from_records(data.get("name", Path(path).stem), data["episodes"], data["questions"])


def load(name: str) -> Dataset:
    if name == "builtin":
        return _BUILTIN
    if name in {"locomo", "longmemeval"}:
        raise NotImplementedError(
            f"To run the {name!r} benchmark: download it, convert it to the JSON shape "
            f'{{"episodes":[{{"text","subject_id","valid_from"}}], '
            f'"questions":[{{"query","subject_id","expected","as_of"?}}]}}, then run '
            f"`python -m eval.harness --dataset-file path/to/{name}.json "
            f"--llm anthropic --engine postgres`. (The LLM extractor is required for "
            f"conversational benchmarks; set ANTHROPIC_API_KEY.)"
        )
    raise ValueError(f"unknown dataset: {name!r}")
