"""Integration tests for the frozen RAGChecker replication runner."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentlab.research.agent import ResearchAgent
from agentlab.research.ragchecker_evaluation import (
    CORPUS_SHA256,
    SUITE_SHA256,
    RagCheckerEvaluationError,
    load_ragchecker_fixture,
    render_ragchecker_report,
    run_ragchecker_evaluation,
)


SUITE_ROOT = Path(__file__).parents[1] / "evals" / "paper-ragchecker-v1"


def test_frozen_fixture_loads_with_pinned_provenance() -> None:
    fixture, corpus = load_ragchecker_fixture(SUITE_ROOT)
    assert fixture.implementation_started_at == "2026-08-03T15:51:45Z"
    assert len(fixture.matrix_cases) == 6
    assert len(fixture.agent_fixture.cases) == 3
    assert len(corpus.sources) == 3
    assert len(SUITE_SHA256) == len(CORPUS_SHA256) == 64


def test_evaluation_runs_real_research_agent_and_diagnoses_error_sources(monkeypatch) -> None:
    fixture, corpus = load_ragchecker_fixture(SUITE_ROOT)
    calls: list[str] = []
    original_run = ResearchAgent.run

    def recording_run(self, question):
        calls.append(question)
        return original_run(self, question)

    monkeypatch.setattr(ResearchAgent, "run", recording_run)
    report = run_ragchecker_evaluation(fixture, corpus)
    assert report.passed is True
    assert report.result == "passed"
    assert len(report.cases) == 9
    assert calls == [fixture.agent_fixture.question] * 3

    cases = {case.id: case for case in report.cases}
    assert cases["agent-clean-context"].agent_contract_passed is True
    assert cases["agent-irrelevant-noise"].metrics.noise_sensitivity_in_irrelevant == 0.5
    assert cases["agent-retrieval-miss"].metrics.claim_recall == 0.5
    assert cases["agent-retrieval-miss"].metrics.context_precision == 1.0


def test_report_is_stable_and_contains_no_corpus_text() -> None:
    fixture, corpus = load_ragchecker_fixture(SUITE_ROOT)
    rendered = render_ragchecker_report(run_ragchecker_evaluation(fixture, corpus))
    parsed = json.loads(rendered)
    assert parsed["scope"] == "oracle-label claim-level metric replication"
    assert parsed["metric_scale"] == "ratio_0_to_1"
    assert parsed["suite_sha256"] == SUITE_SHA256
    assert "The synthetic Atlas" not in rendered
    assert render_ragchecker_report(run_ragchecker_evaluation(fixture, corpus)) == rendered


def test_canonical_report_matches_recomputed_output() -> None:
    fixture, corpus = load_ragchecker_fixture(SUITE_ROOT)
    expected = (SUITE_ROOT / "canonical-report.json").read_text(encoding="utf-8")
    assert render_ragchecker_report(run_ragchecker_evaluation(fixture, corpus)) == expected


def test_suite_hash_drift_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "suite"
    shutil.copytree(SUITE_ROOT, root)
    path = root / "suite-v1.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RagCheckerEvaluationError, match="suite changed"):
        load_ragchecker_fixture(root)


def test_corpus_hash_drift_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "suite"
    shutil.copytree(SUITE_ROOT, root)
    path = root / "corpus" / "atlas-monthly.md"
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(RagCheckerEvaluationError, match="corpus changed"):
        load_ragchecker_fixture(root)
