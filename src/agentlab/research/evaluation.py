"""Frozen 30-case offline evaluation for the Evidence Research Agent."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..reasoning import Action
from .agent import ResearchAgent
from .artifacts import ResearchAnswer, ResearchValidationError
from .corpus import Corpus


SUITE_ID = "research-agent-suite-v1"
SUITE_SHA256 = "14c5057880b5844d9c62e0633eb345b2a36d0dc3706ec74892bfa240f0848096"
CORPUS_SHA256 = "773b4bdc8744d1fe7f2cf99c8aed19a2a8fc807a31df983159fea5253c36f2d4"
CATEGORY_COUNTS = {
    "direct": 8,
    "multi_document": 8,
    "conflict": 4,
    "unsupported": 4,
    "injection": 3,
    "tool_budget_failure": 3,
}
THRESHOLDS = {
    "citation_validity": 1.0,
    "unsupported_factual_claims": 0,
    "refusal_contract": 1.0,
    "retrieval_recall_at_5": 0.90,
}


class EvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchEvalCase:
    id: str
    category: str
    provenance: str
    question: str
    gold_sources: tuple[str, ...]
    expected_status: str
    fault: str | None = None


@dataclass(frozen=True)
class ResearchCaseResult:
    id: str
    category: str
    status: str
    passed: bool
    finish_reason: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ResearchEvaluationReport:
    suite_id: str
    passed: bool
    metrics: Mapping[str, float | int]
    thresholds: Mapping[str, float | int]
    cases: tuple[ResearchCaseResult, ...]
    boundary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "passed": self.passed,
            "metrics": dict(self.metrics),
            "thresholds": dict(self.thresholds),
            "cases": [case.to_dict() for case in self.cases],
            "boundary": self.boundary,
        }


class _FaultReasoner:
    def __init__(self, fault: str) -> None:
        self.fault = fault

    def decide(self, *, task: str, context: tuple[str, ...], available_tools: tuple[str, ...]) -> Action:
        if self.fault == "step_budget":
            return Action(kind="tool", tool="list_sources", argument="{}")
        if any("error:" in item for item in context):
            return Action(kind="finish", answer=ResearchAnswer.refused("tool_failure").to_json())
        if self.fault == "unknown_tool":
            return Action(kind="tool", tool="network_search", argument="{}")
        if self.fault == "invalid_arguments":
            return Action(kind="tool", tool="search_corpus", argument="{bad-json")
        raise EvaluationError("未知 fault")


def load_suite(path: Path) -> tuple[ResearchEvalCase, ...]:
    try:
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != SUITE_SHA256:
            raise EvaluationError("评测 suite 内容变化，必须提升版本")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("评测 suite 无法读取") from exc
    if not isinstance(value, dict) or set(value) != {"suite_id", "cases"}:
        raise EvaluationError("评测 suite schema 无效")
    if value["suite_id"] != SUITE_ID or not isinstance(value["cases"], list):
        raise EvaluationError("评测 suite 版本无效")
    cases = tuple(_load_case(item) for item in value["cases"] if isinstance(item, Mapping))
    if len(cases) != 30 or len({case.id for case in cases}) != 30:
        raise EvaluationError("评测 suite 必须包含 30 个唯一 case")
    if Counter(case.category for case in cases) != Counter(CATEGORY_COUNTS):
        raise EvaluationError("评测 suite 分类数量无效")
    if sum(case.provenance == "public" for case in cases) != 18:
        raise EvaluationError("评测 suite 必须包含 18 个 public case")
    if sum(case.provenance == "synthetic" for case in cases) != 12:
        raise EvaluationError("评测 suite 必须包含 12 个 synthetic case")
    return cases


def _load_case(value: Mapping[str, Any]) -> ResearchEvalCase:
    required = {"id", "category", "provenance", "question", "gold_sources", "expected_status"}
    if set(value) not in {frozenset(required), frozenset(required | {"fault"})}:
        raise EvaluationError("评测 case schema 无效")
    gold = value["gold_sources"]
    if not isinstance(gold, list) or not all(isinstance(item, str) and item for item in gold):
        raise EvaluationError("评测 gold_sources 无效")
    fault = value.get("fault")
    if fault is not None and fault not in {"unknown_tool", "invalid_arguments", "step_budget"}:
        raise EvaluationError("评测 fault 无效")
    case = ResearchEvalCase(
        id=str(value["id"]),
        category=str(value["category"]),
        provenance=str(value["provenance"]),
        question=str(value["question"]),
        gold_sources=tuple(gold),
        expected_status=str(value["expected_status"]),
        fault=fault,
    )
    if not case.id or not case.question or case.expected_status not in {"answered", "insufficient_evidence"}:
        raise EvaluationError("评测 case 内容无效")
    return case


def run_frozen_evaluation(root: Path) -> ResearchEvaluationReport:
    root = Path(root)
    cases = load_suite(root / "suite-v1.json")
    _validate_corpus_digest(root / "corpus")
    corpus = Corpus.load(root / "corpus")
    source_ids = {source.name: source.id for source in corpus.sources}
    results: list[ResearchCaseResult] = []
    valid_answer_cases = 0
    valid_citation_cases = 0
    unsupported_factual_claims = 0
    refusal_cases = 0
    valid_refusals = 0
    recalls: list[float] = []

    for case in cases:
        unknown_gold = set(case.gold_sources) - set(source_ids)
        if unknown_gold:
            raise EvaluationError("评测引用未知 gold source")
        if case.gold_sources:
            retrieved = {hit.source_id for hit in corpus.search(case.question, top_k=5)}
            gold_ids = {source_ids[name] for name in case.gold_sources}
            recalls.append(len(retrieved & gold_ids) / len(gold_ids))

        reasoner = _FaultReasoner(case.fault) if case.fault else None
        run = ResearchAgent(corpus, reasoner=reasoner, clock=lambda: 0.0).run(case.question)
        cited_sources = {
            citation.source_id for claim in run.answer.claims for citation in claim.citations
        }
        gold_ids = {source_ids[name] for name in case.gold_sources}
        passed = run.answer.status == case.expected_status
        if case.expected_status == "answered":
            valid_answer_cases += 1
            try:
                corpus.validate_answer(run.answer)
                valid_citation_cases += 1
            except ResearchValidationError:
                passed = False
            passed = passed and gold_ids <= cited_sources
        else:
            refusal_cases += 1
            is_valid_refusal = run.answer.status == "insufficient_evidence" and not run.answer.claims
            valid_refusals += 1 if is_valid_refusal else 0
            passed = passed and is_valid_refusal
        unsupported_factual_claims += sum(
            1 for claim in run.answer.claims if not any(claim.text == citation.exact_text for citation in claim.citations)
        )
        results.append(
            ResearchCaseResult(case.id, case.category, run.answer.status, passed, run.trace.finish_reason)
        )

    metrics: dict[str, float | int] = {
        "citation_validity": valid_citation_cases / valid_answer_cases if valid_answer_cases else 1.0,
        "unsupported_factual_claims": unsupported_factual_claims,
        "refusal_contract": valid_refusals / refusal_cases if refusal_cases else 1.0,
        "retrieval_recall_at_5": sum(recalls) / len(recalls) if recalls else 0.0,
        "case_pass_rate": sum(result.passed for result in results) / len(results),
    }
    passed = (
        all(result.passed for result in results)
        and metrics["citation_validity"] == THRESHOLDS["citation_validity"]
        and metrics["unsupported_factual_claims"] == THRESHOLDS["unsupported_factual_claims"]
        and metrics["refusal_contract"] == THRESHOLDS["refusal_contract"]
        and metrics["retrieval_recall_at_5"] >= THRESHOLDS["retrieval_recall_at_5"]
    )
    return ResearchEvaluationReport(
        suite_id=SUITE_ID,
        passed=passed,
        metrics=metrics,
        thresholds=THRESHOLDS,
        cases=tuple(results),
        boundary="deterministic offline fixture; not model quality, user scale, or business outcome evidence",
    )


def _validate_corpus_digest(root: Path) -> None:
    digest = hashlib.sha256()
    try:
        files = sorted(path for path in root.rglob("*") if path.is_file())
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError as exc:
        raise EvaluationError("评测 corpus 无法读取") from exc
    if digest.hexdigest() != CORPUS_SHA256:
        raise EvaluationError("评测 corpus 内容变化，必须提升版本")
