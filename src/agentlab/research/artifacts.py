"""Strict output contracts for grounded research answers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


class ResearchValidationError(ValueError):
    """Stable public validation error without source content."""


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ResearchValidationError(f"{label} 字段无效")


@dataclass(frozen=True)
class Citation:
    source_id: str
    chunk_id: str
    start: int
    end: int
    exact_text: str

    def __post_init__(self) -> None:
        if not self.source_id or not self.chunk_id:
            raise ResearchValidationError("citation id 无效")
        if self.start < 0 or self.end <= self.start:
            raise ResearchValidationError("citation span 无效")
        if not self.exact_text:
            raise ResearchValidationError("citation exact_text 为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "start": self.start,
            "end": self.end,
            "exact_text": self.exact_text,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Citation":
        _require_keys(value, {"source_id", "chunk_id", "start", "end", "exact_text"}, "citation")
        if isinstance(value["start"], bool) or not isinstance(value["start"], int):
            raise ResearchValidationError("citation start 无效")
        if isinstance(value["end"], bool) or not isinstance(value["end"], int):
            raise ResearchValidationError("citation end 无效")
        return cls(
            source_id=str(value["source_id"]),
            chunk_id=str(value["chunk_id"]),
            start=value["start"],
            end=value["end"],
            exact_text=str(value["exact_text"]),
        )


@dataclass(frozen=True)
class Claim:
    text: str
    citations: tuple[Citation, ...]

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ResearchValidationError("claim text 为空")
        if not self.citations:
            raise ResearchValidationError("claim 缺少 citation")

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "citations": [citation.to_dict() for citation in self.citations]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Claim":
        _require_keys(value, {"text", "citations"}, "claim")
        citations = value["citations"]
        if not isinstance(citations, list):
            raise ResearchValidationError("claim citations 无效")
        return cls(
            text=str(value["text"]),
            citations=tuple(Citation.from_dict(item) for item in citations if isinstance(item, Mapping)),
        )


@dataclass(frozen=True)
class ResearchAnswer:
    status: str
    answer: str
    claims: tuple[Claim, ...]
    reason: str | None

    def __post_init__(self) -> None:
        if self.status == "answered":
            if not self.answer.strip() or not self.claims or self.reason is not None:
                raise ResearchValidationError("answered 契约无效")
        elif self.status == "insufficient_evidence":
            if self.answer or self.claims or not self.reason:
                raise ResearchValidationError("insufficient_evidence 契约无效")
        else:
            raise ResearchValidationError("answer status 无效")

    @classmethod
    def refused(cls, reason: str) -> "ResearchAnswer":
        return cls(status="insufficient_evidence", answer="", claims=(), reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "claims": [claim.to_dict() for claim in self.claims],
            "reason": self.reason,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "ResearchAnswer":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ResearchValidationError("final answer 不是合法 JSON") from exc
        if not isinstance(value, Mapping):
            raise ResearchValidationError("final answer 不是对象")
        _require_keys(value, {"status", "answer", "claims", "reason"}, "answer")
        claims = value["claims"]
        if not isinstance(claims, list) or not all(isinstance(item, Mapping) for item in claims):
            raise ResearchValidationError("answer claims 无效")
        reason = value["reason"]
        if reason is not None and not isinstance(reason, str):
            raise ResearchValidationError("answer reason 无效")
        return cls(
            status=str(value["status"]),
            answer=str(value["answer"]),
            claims=tuple(Claim.from_dict(item) for item in claims),
            reason=reason,
        )


def unsupported_claim_count(answer: ResearchAnswer) -> int:
    return sum(1 for claim in answer.claims if not claim.citations)
