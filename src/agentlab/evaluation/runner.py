"""Deterministic eval runner over fixed cases."""

from __future__ import annotations

from typing import Callable, Sequence

from ..messages import estimate_tokens
from .case import EvalCase
from .metrics import CaseResult


class EvalRunner:
    """Run cases with injected execution, clock and token counter."""

    def __init__(
        self,
        *,
        execute: Callable[[EvalCase], str],
        clock: Callable[[], float],
        token_counter: Callable[[str], int] = estimate_tokens,
    ) -> None:
        self.execute = execute
        self.clock = clock
        self.token_counter = token_counter

    def run(self, cases: Sequence[EvalCase]) -> tuple[CaseResult, ...]:
        results: list[CaseResult] = []
        for case in cases:
            started = self.clock()
            output = self.execute(case)
            finished = self.clock()
            ok = case.expected_result in output
            quality = 1.0 if ok else 0.0
            results.append(CaseResult(
                case=case,
                ok=ok,
                quality_score=quality,
                completion_seconds=max(0.0, finished - started),
                tokens_used=self.token_counter(case.task) + self.token_counter(output),
                output=output,
            ))
        return tuple(results)
