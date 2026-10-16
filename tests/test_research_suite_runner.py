"""Declarative evaluation suite runner tests.

The external suite is a **diagnostic**, not a regression gate: its refusal metric
records a measured capability boundary of the offline replay reasoner. The test
below pins that measurement so the boundary cannot drift silently in either
direction -- if refusal correctness improves, this test fails and forces the
canonical report, README and docs/known-gaps.md to be updated together.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agentlab.research.corpus import Corpus
from agentlab.research.agent import ResearchAgent
from agentlab.research.suite_runner import (
    FAULT_FINISH_REASONS,
    _AdversarialReasoner,
    SuiteError,
    corpus_digest,
    load_declarative_suite,
    run_declarative_suite,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTERNAL_SUITE = REPO_ROOT / "evals" / "external-v1"
RISK_SUITE = REPO_ROOT / "evals" / "risk-v1"


def _minimal_suite(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("缓存使用 LRU 策略淘汰旧条目。", encoding="utf-8")
    suite = {
        "suite_id": "unit-v1",
        "suite_schema": 1,
        "corpus_sha256": corpus_digest(corpus),
        "provenance": {"name": "unit fixture", "license": "CC0 1.0"},
        "expectations": {"cases": 1, "corpus_files": 1, "by_category": {"direct": 1}},
        "thresholds": {"citation_validity": 1.0},
        "cases": [
            {
                "id": "c1",
                "category": "direct",
                "provenance": "synthetic",
                "question": "缓存用什么策略淘汰旧条目？",
                "gold_sources": ["a.md"],
                "expected_status": "answered",
            }
        ],
    }
    (tmp_path / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_external_suite_records_known_refusal_gap() -> None:
    """Reference the measurement, not a wish: the offline reasoner cannot refuse these."""
    report = run_declarative_suite(EXTERNAL_SUITE)
    assert report.metrics["citation_validity"] == 1.0
    assert report.metrics["unsupported_factual_claims"] == 0
    assert report.metrics["forbidden_tool_calls"] == 0
    assert report.metrics["retrieval_recall_at_5"] >= 0.90
    # Measured 2026-10-16 against crowd-labelled SQuAD 2.0 unanswerable questions.
    assert report.metrics["refusal_contract"] == 0.0
    assert report.passed is False
    assert report.provenance["license"] == "CC BY-SA 4.0"


def test_external_suite_has_external_provenance() -> None:
    manifest, cases = load_declarative_suite(EXTERNAL_SUITE)
    assert manifest["provenance"]["name"].startswith("SQuAD 2.0")
    assert manifest["provenance"]["sha256"]
    assert len(cases) == 90
    assert all(case.provenance == "external" for case in cases)
    assert sum(1 for case in cases if case.expected_status == "insufficient_evidence") == 30


def test_external_report_does_not_leak_questions_or_paths() -> None:
    report = run_declarative_suite(EXTERNAL_SUITE)
    blob = json.dumps(report.to_dict(), ensure_ascii=False)
    manifest, cases = load_declarative_suite(EXTERNAL_SUITE)
    assert all(case.question[:40] not in blob for case in cases)
    assert str(REPO_ROOT) not in blob
    assert "ccc-00.md" in blob, "文件名可以出现：它不含语料正文"


def test_runner_rejects_corpus_digest_drift(tmp_path: Path) -> None:
    root = _minimal_suite(tmp_path)
    (root / "corpus" / "a.md").write_text("被改动过的语料。", encoding="utf-8")
    with pytest.raises(SuiteError, match="digest 漂移"):
        load_declarative_suite(root)


def test_runner_rejects_unknown_or_missing_case_fields(tmp_path: Path) -> None:
    root = _minimal_suite(tmp_path)
    suite = json.loads((root / "suite.json").read_text(encoding="utf-8"))

    for mutate, message in (
        (lambda case: case.update({"unexpected": 1}), "schema"),
        (lambda case: case.pop("expected_status"), "schema"),
        (lambda case: case.update({"expected_status": "maybe"}), "expected_status"),
        (lambda case: case.update({"gold_sources": []}), None),
        (lambda case: case.update({"forbid_tools": "search_corpus"}), "forbid_tools"),
        (lambda case: case.update({"expected_side_effects": -1}), "expected_side_effects"),
    ):
        broken = copy.deepcopy(suite)
        mutate(broken["cases"][0])
        (root / "suite.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        if message is None:
            # 空 gold_sources 对 insufficient_evidence 是合法的，这里只要求能加载
            load_declarative_suite(root)
        else:
            with pytest.raises(SuiteError, match=message):
                load_declarative_suite(root)
    (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")


def test_runner_rejects_case_census_mismatch(tmp_path: Path) -> None:
    root = _minimal_suite(tmp_path)
    suite = json.loads((root / "suite.json").read_text(encoding="utf-8"))
    suite["expectations"]["by_category"] = {"other": 1}
    (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SuiteError, match="category"):
        load_declarative_suite(root)


def test_runner_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    root = _minimal_suite(tmp_path)
    suite = json.loads((root / "suite.json").read_text(encoding="utf-8"))
    suite["suite_schema"] = 99
    (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SuiteError, match="版本不支持"):
        load_declarative_suite(root)


def test_forbidden_tool_and_side_effect_assertions_fail_a_case(tmp_path: Path) -> None:
    """动作级断言必须是可失败的，否则它们只是装饰。"""
    root = _minimal_suite(tmp_path)
    suite = json.loads((root / "suite.json").read_text(encoding="utf-8"))
    suite["cases"][0]["forbid_tools"] = ["search_corpus"]
    (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    report = run_declarative_suite(root)
    assert report.cases[0].forbidden_tools_called == ("search_corpus",)
    assert report.cases[0].passed is False
    assert report.metrics["forbidden_tool_calls"] == 1
    assert report.passed is False

    suite["cases"][0]["forbid_tools"] = []
    suite["cases"][0]["expected_side_effects"] = 0
    (root / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    report = run_declarative_suite(root)
    assert report.cases[0].write_calls == 0, "只读工具不得产生副作用"
    assert report.cases[0].passed is True


def test_every_fault_reaches_its_intended_finish_reason() -> None:
    """防假绿灯。

    一个 fault 如果没能走到预期分支（例如因为参数不够大而只是把步数耗尽），
    case 仍可能因为 expected_status 恰好匹配而"通过"。这条元测试逐条断言终态，
    使这类缺陷无法静默复发。
    """
    from agentlab.research.suite_runner import FAULT_FINISH_REASONS

    corpus = Corpus.load(Path("evals/risk-v1/corpus"))
    question = "What port does the Atlas ingestion service listen on?"
    for fault, expected in sorted(FAULT_FINISH_REASONS.items()):
        run = ResearchAgent(corpus, reasoner=_AdversarialReasoner(fault), clock=lambda: 0.0).run(question)
        assert run.trace.finish_reason == expected, f"{fault} 未到达预期终态：{run.trace.finish_reason}"


def test_risk_suite_fault_cases_use_the_declared_fault_set() -> None:
    manifest, cases = load_declarative_suite(RISK_SUITE)
    from agentlab.research.suite_runner import FAULTS

    used = {case.fault for case in cases if case.fault}
    assert used, "risk suite 必须包含带 fault 的对抗 case"
    assert used <= FAULTS
    assert not (used - set(FAULT_FINISH_REASONS)), "每个 fault 都必须有可断言的终态"


def test_risk_suite_passes_and_forbids_writes_everywhere() -> None:
    report = run_declarative_suite(RISK_SUITE)
    assert report.passed is True
    assert report.metrics["forbidden_tool_calls"] == 0
    assert report.metrics["citation_validity"] == 1.0
    assert report.metrics["refusal_contract"] == 1.0
    manifest, cases = load_declarative_suite(RISK_SUITE)
    assert all(case.expected_side_effects == 0 for case in cases), "全 suite 必须零副作用"
    assert not any(result.write_calls for result in report.cases)
