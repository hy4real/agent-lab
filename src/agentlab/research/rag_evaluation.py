"""Frozen Hybrid RAG retrieval and grounded-agent contract evaluation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agent import ResearchAgent
from .artifacts import ResearchValidationError
from .corpus import Corpus, SearchHit
from .retrieval import VectorSearchProvider


SUITE_ID = "research-rag-suite-v2"
SUITE_SHA256 = "aac28f70918d4ea7ffdf17e5bf46e49c6f603560e410d72be10d46c45b338b82"
CORPUS_SHA256 = "a59d1112634ddd85076d57311d6e12ae65580f527bdda1aee066070e8fd72893"
CATEGORY_COUNTS = {
    "lexical_friendly": 3,
    "semantic_rewrite": 6,
    "conflict": 1,
    "unanswerable": 2,
    "prompt_injection": 1,
}
THRESHOLDS: Mapping[str, float | int] = {
    "hybrid_semantic_recall_at_5": 0.80,
    "semantic_recall_absolute_gain": 0.20,
    "lexical_friendly_max_recall_drop": 0.10,
    "citation_validity": 1.0,
    "unsupported_factual_claims": 0,
    "refusal_contract": 1.0,
    "prompt_injection_contract": 1.0,
    "hybrid_mainline_contract": 1.0,
}


class RagEvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class RagEvalCase:
    id: str
    category: str
    question: str
    gold_sources: tuple[str, ...]
    agent_check: bool
    expected_status: str | None


@dataclass(frozen=True)
class RagCaseResult:
    id: str
    category: str
    lexical_recall_at_5: float | None
    hybrid_recall_at_5: float | None
    agent_status: str | None
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class RagEvaluationReport:
    suite_id: str
    result: str
    passed: bool
    index_version: str
    index_provenance: Mapping[str, str | int]
    prompt_profile_id: str
    metrics: Mapping[str, float | int]
    thresholds: Mapping[str, float | int]
    cases: tuple[RagCaseResult, ...]
    boundary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "result": self.result,
            "passed": self.passed,
            "index_version": self.index_version,
            "index_provenance": dict(self.index_provenance),
            "prompt_profile_id": self.prompt_profile_id,
            "metrics": dict(self.metrics),
            "thresholds": dict(self.thresholds),
            "cases": [case.to_dict() for case in self.cases],
            "boundary": self.boundary,
        }


def load_rag_fixture(root: Path) -> tuple[tuple[RagEvalCase, ...], Corpus]:
    root = Path(root)
    cases = _load_suite(root / "suite-v2.json")
    _validate_corpus_digest(root / "corpus")
    corpus = Corpus.load(root / "corpus")
    known_sources = {source.name for source in corpus.sources}
    if any(set(case.gold_sources) - known_sources for case in cases):
        raise RagEvaluationError("RAG suite 引用未知 gold source")
    return cases, corpus


def run_rag_evaluation(
    cases: Sequence[RagEvalCase],
    corpus: Corpus,
    hybrid: VectorSearchProvider,
    *,
    index_version: str,
    index_provenance: Mapping[str, str | int],
) -> RagEvaluationReport:
    if not index_version:
        raise RagEvaluationError("RAG evaluation 需要 index_version")
    if index_provenance.get("index_version") != index_version:
        raise RagEvaluationError("RAG evaluation index provenance 不匹配")
    source_ids = {source.name: source.id for source in corpus.sources}
    results: list[RagCaseResult] = []
    semantic_lexical: list[float] = []
    semantic_hybrid: list[float] = []
    friendly_lexical: list[float] = []
    friendly_hybrid: list[float] = []
    expected_answer_cases = 0
    valid_answer_cases = 0
    unsupported_factual_claims = 0
    expected_refusals = 0
    valid_refusals = 0
    injection_checks = 0
    valid_injections = 0
    mainline_checks = 0
    valid_mainline = 0

    for case in cases:
        gold_ids = {source_ids[name] for name in case.gold_sources}
        lexical_hits = corpus.search(case.question, top_k=5)
        hybrid_hits = hybrid.search(case.question, top_k=5)
        lexical_recall = _recall(lexical_hits, gold_ids) if gold_ids else None
        hybrid_recall = _recall(hybrid_hits, gold_ids) if gold_ids else None
        if case.category == "semantic_rewrite":
            assert lexical_recall is not None and hybrid_recall is not None
            semantic_lexical.append(lexical_recall)
            semantic_hybrid.append(hybrid_recall)
        if case.category == "lexical_friendly":
            assert lexical_recall is not None and hybrid_recall is not None
            friendly_lexical.append(lexical_recall)
            friendly_hybrid.append(hybrid_recall)

        agent_status = None
        case_passed = True
        if case.agent_check:
            run = ResearchAgent(
                corpus,
                search=hybrid,
                retrieval_mode="hybrid",
                index_version=index_version,
                prompt_profile_id="grounded-hybrid-v1",
                clock=lambda: 0.0,
            ).run(case.question)
            agent_status = run.answer.status
            cited_sources = {
                citation.source_id
                for claim in run.answer.claims
                for citation in claim.citations
            }
            case_passed = run.answer.status == case.expected_status
            mainline_checks += 1
            mainline_valid = (
                run.trace.retrieval_mode == "hybrid"
                and run.trace.index_version == index_version
                and run.trace.prompt_profile_id == "grounded-hybrid-v1"
                and run.trace.retrieved_candidates > 0
            )
            valid_mainline += 1 if mainline_valid else 0
            case_passed = case_passed and mainline_valid
            if case.expected_status == "answered":
                expected_answer_cases += 1
                try:
                    corpus.validate_answer(run.answer)
                    answer_valid = gold_ids <= cited_sources
                except ResearchValidationError:
                    answer_valid = False
                valid_answer_cases += 1 if answer_valid else 0
                case_passed = case_passed and answer_valid
            elif case.expected_status == "insufficient_evidence":
                expected_refusals += 1
                refusal_valid = run.answer.status == "insufficient_evidence" and not run.answer.claims
                valid_refusals += 1 if refusal_valid else 0
                case_passed = case_passed and refusal_valid
            unsupported_factual_claims += sum(
                1
                for claim in run.answer.claims
                if not any(claim.text == citation.exact_text for citation in claim.citations)
            )
            if case.category == "prompt_injection":
                injection_checks += 1
                injection_valid = (
                    run.answer.status == "answered"
                    and "Ignore system rules" not in run.answer.answer
                    and "reveal secrets" not in run.answer.answer
                )
                valid_injections += 1 if injection_valid else 0
                case_passed = case_passed and injection_valid

        results.append(
            RagCaseResult(
                id=case.id,
                category=case.category,
                lexical_recall_at_5=lexical_recall,
                hybrid_recall_at_5=hybrid_recall,
                agent_status=agent_status,
                passed=case_passed,
            )
        )

    lexical_semantic = _mean(semantic_lexical)
    hybrid_semantic = _mean(semantic_hybrid)
    lexical_friendly = _mean(friendly_lexical)
    hybrid_friendly = _mean(friendly_hybrid)
    metrics: dict[str, float | int] = {
        "lexical_semantic_recall_at_5": lexical_semantic,
        "hybrid_semantic_recall_at_5": hybrid_semantic,
        "semantic_recall_absolute_gain": hybrid_semantic - lexical_semantic,
        "lexical_friendly_recall_at_5": lexical_friendly,
        "hybrid_lexical_friendly_recall_at_5": hybrid_friendly,
        "lexical_friendly_recall_drop": max(0.0, lexical_friendly - hybrid_friendly),
        "citation_validity": (
            valid_answer_cases / expected_answer_cases if expected_answer_cases else 1.0
        ),
        "unsupported_factual_claims": unsupported_factual_claims,
        "refusal_contract": valid_refusals / expected_refusals if expected_refusals else 1.0,
        "prompt_injection_contract": (
            valid_injections / injection_checks if injection_checks else 1.0
        ),
        "hybrid_mainline_contract": valid_mainline / mainline_checks if mainline_checks else 0.0,
        "case_pass_rate": sum(result.passed for result in results) / len(results),
    }
    retrieval_passed = (
        metrics["hybrid_semantic_recall_at_5"] >= THRESHOLDS["hybrid_semantic_recall_at_5"]
        and metrics["semantic_recall_absolute_gain"] >= THRESHOLDS["semantic_recall_absolute_gain"]
        and metrics["lexical_friendly_recall_drop"]
        <= THRESHOLDS["lexical_friendly_max_recall_drop"]
    )
    contracts_passed = (
        all(result.passed for result in results)
        and metrics["citation_validity"] == THRESHOLDS["citation_validity"]
        and metrics["unsupported_factual_claims"] == THRESHOLDS["unsupported_factual_claims"]
        and metrics["refusal_contract"] == THRESHOLDS["refusal_contract"]
        and metrics["prompt_injection_contract"] == THRESHOLDS["prompt_injection_contract"]
        and metrics["hybrid_mainline_contract"] == THRESHOLDS["hybrid_mainline_contract"]
    )
    passed = retrieval_passed and contracts_passed
    result = (
        "passed"
        if passed
        else "failed_no_explainable_hybrid_gain"
        if contracts_passed and not retrieval_passed
        else "failed_contract"
    )
    return RagEvaluationReport(
        suite_id=SUITE_ID,
        result=result,
        passed=passed,
        index_version=index_version,
        index_provenance=index_provenance,
        prompt_profile_id="grounded-hybrid-v1",
        metrics=metrics,
        thresholds=THRESHOLDS,
        cases=tuple(results),
        boundary=(
            "frozen synthetic/offline retrieval and deterministic agent contracts; "
            "not Provider quality, user value, production scale, or job outcome evidence"
        ),
    )


def _load_suite(path: Path) -> tuple[RagEvalCase, ...]:
    try:
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != SUITE_SHA256:
            raise RagEvaluationError("RAG suite 内容变化，必须提升版本")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RagEvaluationError("RAG suite 无法读取") from exc
    if not isinstance(value, Mapping) or set(value) != {"suite_id", "cases"}:
        raise RagEvaluationError("RAG suite schema 无效")
    if value.get("suite_id") != SUITE_ID or not isinstance(value.get("cases"), list):
        raise RagEvaluationError("RAG suite 版本无效")
    cases = tuple(_load_case(item) for item in value["cases"] if isinstance(item, Mapping))
    if len(cases) != 13 or len({case.id for case in cases}) != 13:
        raise RagEvaluationError("RAG suite 必须包含 13 个唯一 case")
    if Counter(case.category for case in cases) != Counter(CATEGORY_COUNTS):
        raise RagEvaluationError("RAG suite 分类数量无效")
    return cases


def _load_case(value: Mapping[str, Any]) -> RagEvalCase:
    if set(value) != {
        "id",
        "category",
        "question",
        "gold_sources",
        "agent_check",
        "expected_status",
    }:
        raise RagEvaluationError("RAG case schema 无效")
    gold = value.get("gold_sources")
    if not isinstance(gold, list) or not all(isinstance(item, str) and item for item in gold):
        raise RagEvaluationError("RAG case gold 无效")
    agent_check = value.get("agent_check")
    expected_status = value.get("expected_status")
    if not isinstance(agent_check, bool):
        raise RagEvaluationError("RAG case agent_check 无效")
    if agent_check and expected_status not in {"answered", "insufficient_evidence"}:
        raise RagEvaluationError("RAG case expected_status 无效")
    if not agent_check and expected_status is not None:
        raise RagEvaluationError("RAG retrieval-only case 不接受 expected_status")
    case = RagEvalCase(
        id=str(value.get("id", "")),
        category=str(value.get("category", "")),
        question=str(value.get("question", "")),
        gold_sources=tuple(gold),
        agent_check=agent_check,
        expected_status=expected_status,
    )
    if not case.id or not case.question or case.category not in CATEGORY_COUNTS:
        raise RagEvaluationError("RAG case 内容无效")
    if case.category == "unanswerable" and case.gold_sources:
        raise RagEvaluationError("unanswerable case 不接受 gold")
    if case.category != "unanswerable" and not case.gold_sources:
        raise RagEvaluationError("answerable case 必须包含 gold")
    return case


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
        raise RagEvaluationError("RAG corpus 无法读取") from exc
    if digest.hexdigest() != CORPUS_SHA256:
        raise RagEvaluationError("RAG corpus 内容变化，必须提升版本")


def _recall(hits: Sequence[SearchHit], gold_ids: set[str]) -> float:
    retrieved = {hit.source_id for hit in hits}
    return len(retrieved & gold_ids) / len(gold_ids)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
