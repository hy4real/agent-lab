"""Minimal, provider-agnostic message contract for Agent Lab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ROLES = frozenset({"system", "user", "assistant", "tool"})


@dataclass(frozen=True)
class Message:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"role 必须是 {sorted(ROLES)} 之一: {self.role!r}")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content 必须是非空字符串")

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Message":
        return cls(role=str(value["role"]), content=str(value["content"]))


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    """Deterministic token heuristic (chars / chars_per_token, ceiling).

    This is an explicit approximation for offline determinism, not a model
    tokenizer. Production boundaries must never claim exact token counts.
    """
    if chars_per_token <= 0:
        raise ValueError("chars_per_token 必须为正整数")
    if not text:
        return 0
    return (len(text) + chars_per_token - 1) // chars_per_token
