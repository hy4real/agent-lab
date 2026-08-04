"""Fixed-case regression suite with baseline snapshot comparison."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Sequence

from .case import EvalCase
from .metrics import CaseResult
from .runner import EvalRunner


@dataclass(frozen=True)
class RegressionReport:
    passed: int
    failed: int
    snapshot_hash: str
    baseline_match: bool | None
    failures: tuple[str, ...]


def _snapshot_hash(results: Sequence[CaseResult]) -> str:
    payload = json.dumps(
        [{"id": result.case.id, "ok": result.ok} for result in results],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RegressionSuite:
    def __init__(self, *, cases: Sequence[EvalCase], baseline_hash: str | None = None) -> None:
        if not cases:
            raise ValueError("回归套件至少需要一个 case")
        self.cases = tuple(cases)
        self.baseline_hash = baseline_hash

    def run(self, runner: EvalRunner) -> RegressionReport:
        results = runner.run(self.cases)
        failures = tuple(result.case.id for result in results if not result.ok)
        snapshot = _snapshot_hash(results)
        baseline_match = snapshot == self.baseline_hash if self.baseline_hash is not None else None
        return RegressionReport(
            passed=len(results) - len(failures),
            failed=len(failures),
            snapshot_hash=snapshot,
            baseline_match=baseline_match,
            failures=failures,
        )

    def freeze(self, runner: EvalRunner) -> str:
        """Produce the baseline hash for the current deterministic run."""
        return self.run(runner).snapshot_hash
