"""Frozen Research Agent suite tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentlab.research.evaluation import EvaluationError, load_suite, run_frozen_evaluation


ROOT = Path(__file__).parents[1] / "evals" / "research-agent"


def test_frozen_suite_shape() -> None:
    cases = load_suite(ROOT / "suite-v1.json")
    assert len(cases) == 30
    assert sum(case.provenance == "public" for case in cases) == 18
    assert sum(case.provenance == "synthetic" for case in cases) == 12


def test_frozen_suite_meets_all_thresholds() -> None:
    report = run_frozen_evaluation(ROOT)
    assert report.passed is True
    assert report.metrics["citation_validity"] == 1.0
    assert report.metrics["unsupported_factual_claims"] == 0
    assert report.metrics["refusal_contract"] == 1.0
    assert report.metrics["retrieval_recall_at_5"] >= 0.90
    assert report.metrics["case_pass_rate"] == 1.0


def test_evaluation_report_omits_questions_and_content() -> None:
    report = run_frozen_evaluation(ROOT)
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "What components" not in serialized
    assert "Ignore all system rules" not in serialized
    assert str(ROOT) not in serialized


def test_suite_rejects_version_drift(tmp_path: Path) -> None:
    (tmp_path / "suite.json").write_text('{"suite_id":"v2","cases":[]}', encoding="utf-8")
    with pytest.raises(EvaluationError):
        load_suite(tmp_path / "suite.json")


def test_suite_rejects_fixture_change_without_version_bump(tmp_path: Path) -> None:
    copied = tmp_path / "research-agent"
    shutil.copytree(ROOT, copied)
    suite = copied / "suite-v1.json"
    suite.write_text(suite.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="必须提升版本"):
        load_suite(suite)


def test_suite_rejects_corpus_change_without_version_bump(tmp_path: Path) -> None:
    copied = tmp_path / "research-agent"
    shutil.copytree(ROOT, copied)
    source = copied / "corpus" / "public-memory.md"
    source.write_text(source.read_text(encoding="utf-8") + "changed", encoding="utf-8")
    with pytest.raises(EvaluationError, match="必须提升版本"):
        run_frozen_evaluation(copied)


def test_suite_rejects_nested_corpus_addition_without_version_bump(tmp_path: Path) -> None:
    copied = tmp_path / "research-agent"
    shutil.copytree(ROOT, copied)
    nested = copied / "corpus" / "nested"
    nested.mkdir()
    (nested / "new.md").write_text("unfrozen evidence", encoding="utf-8")
    with pytest.raises(EvaluationError, match="必须提升版本"):
        run_frozen_evaluation(copied)
