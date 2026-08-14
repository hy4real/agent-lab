"""Frozen controlled-memory evaluation contracts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentlab.controlled_memory_evaluation import (
    ControlledMemoryEvaluationError,
    _parse_cases,
    _parse_records,
    _parse_thresholds,
    _rate,
    run_controlled_memory_evaluation,
)


SUITE_ROOT = Path(__file__).parents[1] / "evals" / "controlled-memory-v1"


def test_frozen_report_recomputes_exactly() -> None:
    report = run_controlled_memory_evaluation(SUITE_ROOT)
    expected_json = json.loads((SUITE_ROOT / "baseline-report.json").read_text(encoding="utf-8"))
    expected_markdown = (SUITE_ROOT / "baseline-report.md").read_text(encoding="utf-8")
    assert report.passed is True
    assert report.to_dict() == expected_json
    assert report.to_markdown() == expected_markdown
    assert set(report.metrics.values()) == {1.0}
    assert all(report.state_probes.values())


def test_wrong_expected_record_fails_without_lowering_threshold(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    shutil.copytree(SUITE_ROOT, suite)
    cases = json.loads((suite / "cases.json").read_text(encoding="utf-8"))
    cases["cases"][0]["expected_record_keys"] = ["alpha-offline-preference"]
    (suite / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    report = run_controlled_memory_evaluation(suite)
    assert report.passed is False
    assert report.failures == ("expected_recall_rate",)
    assert report.metrics["expected_recall_rate"] < 1.0


def test_missing_fixture_fails_with_stable_error(tmp_path: Path) -> None:
    with pytest.raises(ControlledMemoryEvaluationError, match="无法读取"):
        run_controlled_memory_evaluation(tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"suite_id": "wrong", "suite_version": 1, "records": []},
        {"suite_id": "controlled-memory-v1", "suite_version": 1, "records": []},
        {
            "suite_id": "controlled-memory-v1",
            "suite_version": 1,
            "records": [{"key": "x"}],
        },
    ],
)
def test_invalid_record_fixture_is_rejected(value) -> None:
    with pytest.raises(ControlledMemoryEvaluationError):
        _parse_records(value)


def test_duplicate_record_key_is_rejected() -> None:
    record = {"key": "x", "kind": "note", "text": "x", "scope": "local", "status": "confirmed"}
    with pytest.raises(ControlledMemoryEvaluationError, match="record"):
        _parse_records({
            "suite_id": "controlled-memory-v1",
            "suite_version": 1,
            "records": [record, record],
        })


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"suite_id": "wrong", "cases": []},
        {"suite_id": "controlled-memory-v1", "cases": []},
        {"suite_id": "controlled-memory-v1", "cases": [{"id": "x"}]},
        {
            "suite_id": "controlled-memory-v1",
            "cases": [{
                "id": "x",
                "query": "q",
                "scope": "local",
                "top_k": 6,
                "expected_record_keys": [],
            }],
        },
        {
            "suite_id": "controlled-memory-v1",
            "cases": [{
                "id": "x",
                "query": "q",
                "scope": "local",
                "top_k": 1,
                "expected_record_keys": ["missing"],
            }],
        },
    ],
)
def test_invalid_case_fixture_is_rejected(value) -> None:
    with pytest.raises(ControlledMemoryEvaluationError):
        _parse_cases(value, record_keys=frozenset({"known"}))


def test_duplicate_case_id_is_rejected() -> None:
    case = {
        "id": "x",
        "query": "q",
        "scope": "local",
        "top_k": 1,
        "expected_record_keys": [],
    }
    with pytest.raises(ControlledMemoryEvaluationError, match="case"):
        _parse_cases(
            {"suite_id": "controlled-memory-v1", "cases": [case, case]},
            record_keys=frozenset(),
        )


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"suite_id": "wrong", "metrics": {}},
        {"suite_id": "controlled-memory-v1", "metrics": {}},
    ],
)
def test_invalid_threshold_fixture_is_rejected(value) -> None:
    with pytest.raises(ControlledMemoryEvaluationError):
        _parse_thresholds(value)


def test_out_of_range_threshold_is_rejected() -> None:
    metrics = {
        "expected_recall_rate": 1.1,
        "scope_isolation_rate": 1.0,
        "unconfirmed_exclusion_rate": 1.0,
        "budget_compliance_rate": 1.0,
        "injection_boundary_rate": 1.0,
        "determinism_rate": 1.0,
        "state_contract_rate": 1.0,
    }
    with pytest.raises(ControlledMemoryEvaluationError, match="threshold"):
        _parse_thresholds({"suite_id": "controlled-memory-v1", "metrics": metrics})


def test_rate_rejects_empty_input() -> None:
    with pytest.raises(ControlledMemoryEvaluationError, match="无案例"):
        _rate(())
