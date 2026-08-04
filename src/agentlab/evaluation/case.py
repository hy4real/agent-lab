"""Fixed evaluation case contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    id: str
    task: str
    expected_result: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("case id 无效")
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("case task 无效")
        if not isinstance(self.expected_result, str) or not self.expected_result.strip():
            raise ValueError("case expected_result 无效")
