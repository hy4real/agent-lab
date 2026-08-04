"""Per-case results and aggregate metrics (deterministic)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .case import EvalCase


@dataclass(frozen=True)
class CaseResult:
    case: EvalCase
    ok: bool
    quality_score: float
    completion_seconds: float
    tokens_used: int
    output: str


def aggregate(results: Sequence[CaseResult]) -> dict[str, float | int]:
    total = len(results)
    ok = sum(result.ok for result in results)
    return {
        "total": total,
        "ok": ok,
        "success_rate": ok / total if total else 0.0,
        "avg_quality": sum(result.quality_score for result in results) / total if total else 0.0,
        "total_tokens": sum(result.tokens_used for result in results),
        "avg_completion_seconds": sum(result.completion_seconds for result in results) / total if total else 0.0,
    }


def render_report(results: Sequence[CaseResult], *, boundary_note: str = "") -> str:
    lines = ["case | ok | quality | seconds | tokens"]
    for result in results:
        lines.append(
            f"{result.case.id} | {result.ok} | {result.quality_score:.1f} | "
            f"{result.completion_seconds:.2f} | {result.tokens_used}"
        )
    lines.append(f"aggregate: {aggregate(results)}")
    if boundary_note:
        lines.append(f"boundary: {boundary_note}")
    return "\n".join(lines)
