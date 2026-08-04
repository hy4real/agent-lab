"""Evaluation harness: regression, metrics and observability."""

from .case import EvalCase
from .metrics import CaseResult, aggregate, render_report
from .observability import aggregate_runs, render_observability
from .regression import RegressionReport, RegressionSuite
from .runner import EvalRunner

__all__ = [
    "CaseResult",
    "EvalCase",
    "EvalRunner",
    "RegressionReport",
    "RegressionSuite",
    "aggregate",
    "aggregate_runs",
    "render_observability",
    "render_report",
]
