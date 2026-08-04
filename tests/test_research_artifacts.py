"""Strict Research Agent artifact tests."""

from __future__ import annotations

import json

import pytest

from agentlab.research.artifacts import Citation, Claim, ResearchAnswer, ResearchValidationError


def test_answer_round_trip_is_canonical() -> None:
    citation = Citation("source-a", "chunk-a", 0, 4, "事实文本")
    answer = ResearchAnswer("answered", "事实文本", (Claim("事实文本", (citation,)),), None)
    assert ResearchAnswer.from_json(answer.to_json()) == answer
    assert answer.to_json() == answer.to_json()


@pytest.mark.parametrize(
    "value",
    [
        {"status": "answered", "answer": "x", "claims": [], "reason": None},
        {"status": "insufficient_evidence", "answer": "x", "claims": [], "reason": "none"},
        {"status": "unknown", "answer": "", "claims": [], "reason": "none"},
        {"status": "insufficient_evidence", "answer": "", "claims": [], "reason": "none", "extra": 1},
    ],
)
def test_answer_rejects_invalid_contract(value) -> None:
    with pytest.raises(ResearchValidationError):
        ResearchAnswer.from_json(json.dumps(value))


def test_citation_rejects_invalid_span_and_boolean_offset() -> None:
    with pytest.raises(ResearchValidationError):
        Citation("source-a", "chunk-a", 2, 2, "x")
    value = {"source_id": "s", "chunk_id": "c", "start": True, "end": 1, "exact_text": "x"}
    with pytest.raises(ResearchValidationError):
        Citation.from_dict(value)
