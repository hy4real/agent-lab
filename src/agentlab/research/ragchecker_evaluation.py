"""Frozen oracle-label evaluation for the RAGChecker metric replication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .agent import ResearchAgent
from .corpus import Chunk, Corpus, SearchHit
from .ragchecker_metrics import (
    METRIC_NAMES,
    ClaimEntailmentMatrices,
    RagCheckerMetricError,
    RagCheckerMetrics,
    compute_ragchecker_metrics,
    metrics_match,
)


SUITE_ID = "paper-ragchecker-suite-v1"
SUITE_SHA256 = "e473b9908d2e409cc8fc4f5511ab8e6145d4fc1a06bc3e9f506240d6a282f061"
CORPUS_SHA256 = "42ed69e6f9b3e9a5b6ae2f868911a571a1643d1ddfc4baa99061e31f89f112cb"
PAPER_METADATA = {
    "title": "RAGChecker: A Fine-grained Framework for Diagnosing Retrieval-Augmented Generation",
    "arxiv_id": "2408.08067",
    "paper_version": "v2",
    "doi": "10.52202/079017-0692",
    "official_repository": "amazon-science/RAGChecker",
    "official_commit": "6091f08c00e676e87a970f2aeb4a23a484746348",
}
TOLERANCE = 1e-12


class RagCheckerEvaluationError(ValueError):
    """Stable error for a malformed or stale paper-replication fixture."""


@dataclass(frozen=True)
class MatrixFixtureCase:
    id: str
    matrices: ClaimEntailmentMatrices
    expected_metrics: RagCheckerMetrics


@dataclass(frozen=True)
class OracleGroundTruthClaim:
    id: str
    entailed_by_response_claim_ids: tuple[str, ...]
    entailed_by_sources: tuple[str, ...]


@dataclass(frozen=True)
class OracleResponseClaim:
    id: str
    exact_text: str
    entailed_ground_truth_claim_ids: tuple[str, ...]
    entailed_by_sources: tuple[str, ...]


@dataclass(frozen=True)
class RetrievedSource:
    name: str
    score: float


@dataclass(frozen=True)
class AgentFixtureCase:
    id: str
    retrieved_sources: tuple[RetrievedSource, ...]
    expected_response_claim_ids: tuple[str, ...]
    expected_metrics: RagCheckerMetrics


@dataclass(frozen=True)
class AgentFixture:
    question: str
    ground_truth_claims: tuple[OracleGroundTruthClaim, ...]
    response_claims: tuple[OracleResponseClaim, ...]
    cases: tuple[AgentFixtureCase, ...]


@dataclass(frozen=True)
class RagCheckerFixture:
    implementation_started_at: str
    matrix_cases: tuple[MatrixFixtureCase, ...]
    agent_fixture: AgentFixture


@dataclass(frozen=True)
class RagCheckerCaseResult:
    id: str
    kind: str
    passed: bool
    metrics: RagCheckerMetrics
    expected_metrics: RagCheckerMetrics
    retrieved_sources: tuple[str, ...]
    observed_response_claim_ids: tuple[str, ...]
    agent_contract_passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "passed": self.passed,
            "metrics": self.metrics.to_dict(),
            "expected_metrics": self.expected_metrics.to_dict(),
            "retrieved_sources": list(self.retrieved_sources),
            "observed_response_claim_ids": list(self.observed_response_claim_ids),
            "agent_contract_passed": self.agent_contract_passed,
        }


@dataclass(frozen=True)
class RagCheckerEvaluationReport:
    result: str
    passed: bool
    implementation_started_at: str
    cases: tuple[RagCheckerCaseResult, ...]
    macro_metrics: Mapping[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "suite_id": SUITE_ID,
            "suite_sha256": SUITE_SHA256,
            "corpus_sha256": CORPUS_SHA256,
            "paper": dict(PAPER_METADATA),
            "scope": "oracle-label claim-level metric replication",
            "result": self.result,
            "passed": self.passed,
            "implementation_started_at": self.implementation_started_at,
            "metric_scale": "ratio_0_to_1",
            "tolerance": TOLERANCE,
            "macro_metrics": dict(self.macro_metrics),
            "cases": [case.to_dict() for case in self.cases],
            "boundary": (
                "frozen synthetic/offline oracle labels; not claim extraction, entailment model, "
                "complete RAGChecker, paper conclusion, Provider quality, or user-value evidence"
            ),
        }


class _FrozenSearch:
    def __init__(self, question: str, hits: tuple[SearchHit, ...]) -> None:
        self.question = question
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, top_k: int = 5) -> tuple[SearchHit, ...]:
        if query != self.question:
            raise RagCheckerEvaluationError("frozen search query mismatch")
        self.calls.append((query, top_k))
        return self.hits[:top_k]


def load_ragchecker_fixture(root: Path) -> tuple[RagCheckerFixture, Corpus]:
    root = Path(root)
    raw = _read_frozen_file(root / "suite-v1.json", SUITE_SHA256, "suite")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RagCheckerEvaluationError("suite unreadable") from exc
    fixture = _parse_fixture(_require_mapping(value, "suite"))
    _validate_corpus_digest(root / "corpus")
    corpus = Corpus.load(root / "corpus")
    _validate_agent_fixture(fixture.agent_fixture, corpus)
    return fixture, corpus


def run_ragchecker_evaluation(
    fixture: RagCheckerFixture,
    corpus: Corpus,
) -> RagCheckerEvaluationReport:
    results = [_run_matrix_case(case) for case in fixture.matrix_cases]
    results.extend(_run_agent_case(fixture.agent_fixture, case, corpus) for case in fixture.agent_fixture.cases)
    case_results = tuple(results)
    macro_metrics = {
        name: sum(case.metrics.to_dict()[name] for case in case_results) / len(case_results)
        for name in METRIC_NAMES
    }
    passed = all(case.passed for case in case_results)
    return RagCheckerEvaluationReport(
        result="passed" if passed else "failed_metric_or_agent_contract",
        passed=passed,
        implementation_started_at=fixture.implementation_started_at,
        cases=case_results,
        macro_metrics=macro_metrics,
    )


def render_ragchecker_report(report: RagCheckerEvaluationReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _run_matrix_case(case: MatrixFixtureCase) -> RagCheckerCaseResult:
    metrics = compute_ragchecker_metrics(case.matrices)
    return RagCheckerCaseResult(
        id=case.id,
        kind="matrix",
        passed=metrics_match(metrics, case.expected_metrics, tolerance=TOLERANCE),
        metrics=metrics,
        expected_metrics=case.expected_metrics,
        retrieved_sources=(),
        observed_response_claim_ids=(),
        agent_contract_passed=None,
    )


def _run_agent_case(
    fixture: AgentFixture,
    case: AgentFixtureCase,
    corpus: Corpus,
) -> RagCheckerCaseResult:
    sources = {source.name: source for source in corpus.sources}
    chunks = {source.name: _single_chunk(corpus, source.id) for source in corpus.sources}
    hits = tuple(
        SearchHit(
            chunk_id=chunks[item.name].id,
            source_id=sources[item.name].id,
            source_name=item.name,
            score=item.score,
        )
        for item in case.retrieved_sources
    )
    search = _FrozenSearch(fixture.question, hits)
    run = ResearchAgent(corpus, search=search, clock=lambda: 0.0).run(fixture.question)
    response_by_text = {claim.exact_text: claim for claim in fixture.response_claims}
    observed: list[OracleResponseClaim] = []
    citation_sources: dict[str, set[str]] = {}
    source_names = {source.id: source.name for source in corpus.sources}
    for claim in run.answer.claims:
        definition = response_by_text.get(claim.text)
        if definition is None:
            raise RagCheckerEvaluationError("agent produced unregistered claim")
        observed.append(definition)
        citation_sources[definition.id] = {
            source_names[citation.source_id] for citation in claim.citations
        }

    observed_ids = tuple(claim.id for claim in observed)
    retrieved_names = tuple(item.name for item in case.retrieved_sources)
    agent_contract_passed = (
        run.answer.status == "answered"
        and observed_ids == case.expected_response_claim_ids
        and search.calls == [(fixture.question, 5)]
        and run.trace.retrieved_candidates == len(hits)
        and run.trace.read_chunks == len(hits)
        and all(
            citation_sources[claim.id] <= set(claim.entailed_by_sources)
            for claim in observed
        )
    )
    ground_truth_ids = tuple(claim.id for claim in fixture.ground_truth_claims)
    matrices = ClaimEntailmentMatrices(
        ground_truth_claim_ids=ground_truth_ids,
        response_claim_ids=observed_ids,
        retrieved_chunk_ids=tuple(hit.chunk_id for hit in hits),
        answer2response=tuple(bool(claim.entailed_ground_truth_claim_ids) for claim in observed),
        response2answer=tuple(
            any(claim.id in response.entailed_ground_truth_claim_ids for response in observed)
            for claim in fixture.ground_truth_claims
        ),
        retrieved2answer=tuple(
            tuple(source in claim.entailed_by_sources for source in retrieved_names)
            for claim in fixture.ground_truth_claims
        ),
        retrieved2response=tuple(
            tuple(source in claim.entailed_by_sources for source in retrieved_names)
            for claim in observed
        ),
    )
    metrics = compute_ragchecker_metrics(matrices)
    return RagCheckerCaseResult(
        id=case.id,
        kind="research_agent",
        passed=agent_contract_passed and metrics_match(metrics, case.expected_metrics, tolerance=TOLERANCE),
        metrics=metrics,
        expected_metrics=case.expected_metrics,
        retrieved_sources=retrieved_names,
        observed_response_claim_ids=observed_ids,
        agent_contract_passed=agent_contract_passed,
    )


def _parse_fixture(value: Mapping[str, Any]) -> RagCheckerFixture:
    _require_keys(
        value,
        {"schema_version", "suite_id", "paper", "implementation_started_at", "matrix_cases", "agent_fixture"},
        "suite",
    )
    if value["schema_version"] != 1 or value["suite_id"] != SUITE_ID:
        raise RagCheckerEvaluationError("suite version invalid")
    if _require_mapping(value["paper"], "paper") != PAPER_METADATA:
        raise RagCheckerEvaluationError("paper provenance invalid")
    matrix_values = _require_list(value["matrix_cases"], "matrix_cases")
    matrix_cases = tuple(_parse_matrix_case(_require_mapping(item, "matrix_case")) for item in matrix_values)
    if len(matrix_cases) != 6 or len({case.id for case in matrix_cases}) != 6:
        raise RagCheckerEvaluationError("matrix case set invalid")
    agent_fixture = _parse_agent_fixture(_require_mapping(value["agent_fixture"], "agent_fixture"))
    started_at = _require_string(value["implementation_started_at"], "implementation_started_at")
    return RagCheckerFixture(started_at, matrix_cases, agent_fixture)


def _parse_matrix_case(value: Mapping[str, Any]) -> MatrixFixtureCase:
    expected_keys = {
        "id",
        "ground_truth_claim_ids",
        "response_claim_ids",
        "retrieved_chunk_ids",
        "answer2response",
        "response2answer",
        "retrieved2answer",
        "retrieved2response",
        "expected_metrics",
    }
    _require_keys(value, expected_keys, "matrix_case")
    try:
        matrices = ClaimEntailmentMatrices(
            ground_truth_claim_ids=_string_tuple(value["ground_truth_claim_ids"], "ground_truth_claim_ids"),
            response_claim_ids=_string_tuple(value["response_claim_ids"], "response_claim_ids"),
            retrieved_chunk_ids=_string_tuple(value["retrieved_chunk_ids"], "retrieved_chunk_ids"),
            answer2response=_bool_tuple(value["answer2response"], "answer2response"),
            response2answer=_bool_tuple(value["response2answer"], "response2answer"),
            retrieved2answer=_bool_matrix(value["retrieved2answer"], "retrieved2answer"),
            retrieved2response=_bool_matrix(value["retrieved2response"], "retrieved2response"),
        )
        expected = RagCheckerMetrics.from_mapping(_require_mapping(value["expected_metrics"], "expected_metrics"))
    except RagCheckerMetricError as exc:
        raise RagCheckerEvaluationError("matrix case contract invalid") from exc
    return MatrixFixtureCase(_require_string(value["id"], "matrix case id"), matrices, expected)


def _parse_agent_fixture(value: Mapping[str, Any]) -> AgentFixture:
    _require_keys(value, {"question", "ground_truth_claims", "response_claims", "cases"}, "agent_fixture")
    ground_truth = tuple(
        _parse_ground_truth_claim(_require_mapping(item, "ground_truth_claim"))
        for item in _require_list(value["ground_truth_claims"], "ground_truth_claims")
    )
    responses = tuple(
        _parse_response_claim(_require_mapping(item, "response_claim"))
        for item in _require_list(value["response_claims"], "response_claims")
    )
    cases = tuple(
        _parse_agent_case(_require_mapping(item, "agent_case"))
        for item in _require_list(value["cases"], "agent_cases")
    )
    if len(ground_truth) != 2 or len(responses) != 3 or len(cases) != 3:
        raise RagCheckerEvaluationError("agent fixture set invalid")
    return AgentFixture(_require_string(value["question"], "question"), ground_truth, responses, cases)


def _parse_ground_truth_claim(value: Mapping[str, Any]) -> OracleGroundTruthClaim:
    _require_keys(value, {"id", "entailed_by_response_claim_ids", "entailed_by_sources"}, "ground_truth_claim")
    return OracleGroundTruthClaim(
        _require_string(value["id"], "ground truth claim id"),
        _string_tuple(value["entailed_by_response_claim_ids"], "entailed_by_response_claim_ids"),
        _string_tuple(value["entailed_by_sources"], "entailed_by_sources"),
    )


def _parse_response_claim(value: Mapping[str, Any]) -> OracleResponseClaim:
    _require_keys(
        value,
        {"id", "exact_text", "entailed_ground_truth_claim_ids", "entailed_by_sources"},
        "response_claim",
    )
    return OracleResponseClaim(
        _require_string(value["id"], "response claim id"),
        _require_string(value["exact_text"], "response exact_text"),
        _string_tuple(value["entailed_ground_truth_claim_ids"], "entailed_ground_truth_claim_ids"),
        _string_tuple(value["entailed_by_sources"], "entailed_by_sources"),
    )


def _parse_agent_case(value: Mapping[str, Any]) -> AgentFixtureCase:
    _require_keys(value, {"id", "retrieved_sources", "expected_response_claim_ids", "expected_metrics"}, "agent_case")
    retrieved = tuple(
        _parse_retrieved_source(_require_mapping(item, "retrieved_source"))
        for item in _require_list(value["retrieved_sources"], "retrieved_sources")
    )
    try:
        expected = RagCheckerMetrics.from_mapping(_require_mapping(value["expected_metrics"], "expected_metrics"))
    except RagCheckerMetricError as exc:
        raise RagCheckerEvaluationError("agent expected metrics invalid") from exc
    return AgentFixtureCase(
        _require_string(value["id"], "agent case id"),
        retrieved,
        _string_tuple(value["expected_response_claim_ids"], "expected_response_claim_ids"),
        expected,
    )


def _parse_retrieved_source(value: Mapping[str, Any]) -> RetrievedSource:
    _require_keys(value, {"name", "score"}, "retrieved_source")
    score = value["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 < float(score) <= 1.0:
        raise RagCheckerEvaluationError("retrieved source score invalid")
    return RetrievedSource(_require_string(value["name"], "retrieved source name"), float(score))


def _validate_agent_fixture(fixture: AgentFixture, corpus: Corpus) -> None:
    source_names = {source.name for source in corpus.sources}
    ground_truth_ids = _unique_ids((claim.id for claim in fixture.ground_truth_claims), "ground truth claims")
    response_ids = _unique_ids((claim.id for claim in fixture.response_claims), "response claims")
    _unique_ids((claim.exact_text for claim in fixture.response_claims), "response claim text")
    _unique_ids((case.id for case in fixture.cases), "agent cases")
    for ground_truth_claim in fixture.ground_truth_claims:
        if (
            set(ground_truth_claim.entailed_by_response_claim_ids) - response_ids
            or set(ground_truth_claim.entailed_by_sources) - source_names
        ):
            raise RagCheckerEvaluationError("ground truth oracle link invalid")
    for response_claim in fixture.response_claims:
        if (
            set(response_claim.entailed_ground_truth_claim_ids) - ground_truth_ids
            or set(response_claim.entailed_by_sources) - source_names
        ):
            raise RagCheckerEvaluationError("response oracle link invalid")
    for ground_truth in fixture.ground_truth_claims:
        reverse = {
            response.id
            for response in fixture.response_claims
            if ground_truth.id in response.entailed_ground_truth_claim_ids
        }
        if reverse != set(ground_truth.entailed_by_response_claim_ids):
            raise RagCheckerEvaluationError("oracle entailment links are asymmetric")
    for case in fixture.cases:
        retrieved_names = tuple(item.name for item in case.retrieved_sources)
        if len(set(retrieved_names)) != len(retrieved_names) or set(retrieved_names) - source_names:
            raise RagCheckerEvaluationError("agent retrieved source set invalid")
        if set(case.expected_response_claim_ids) - response_ids:
            raise RagCheckerEvaluationError("agent expected response set invalid")
    for source in corpus.sources:
        _single_chunk(corpus, source.id)


def _single_chunk(corpus: Corpus, source_id: str) -> Chunk:
    chunks = tuple(chunk for chunk in corpus.chunks if chunk.source_id == source_id)
    if len(chunks) != 1:
        raise RagCheckerEvaluationError("paper fixture source must have one chunk")
    return chunks[0]


def _read_frozen_file(path: Path, digest: str, label: str) -> bytes:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RagCheckerEvaluationError(f"{label} unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise RagCheckerEvaluationError(f"{label} changed; bump suite version")
    return raw


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
        raise RagCheckerEvaluationError("corpus unreadable") from exc
    if digest.hexdigest() != CORPUS_SHA256:
        raise RagCheckerEvaluationError("corpus changed; bump suite version")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RagCheckerEvaluationError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RagCheckerEvaluationError(f"{label} must be a list")
    return value


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RagCheckerEvaluationError(f"{label} fields invalid")


def _require_string(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise RagCheckerEvaluationError(f"{label} invalid")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    values = _require_list(value, label)
    result = tuple(_require_string(item, label) for item in values)
    if len(set(result)) != len(result):
        raise RagCheckerEvaluationError(f"{label} contains duplicates")
    return result


def _bool_tuple(value: Any, label: str) -> tuple[bool, ...]:
    values = _require_list(value, label)
    if any(type(item) is not bool for item in values):
        raise RagCheckerEvaluationError(f"{label} must contain bool values")
    return tuple(values)


def _bool_matrix(value: Any, label: str) -> tuple[tuple[bool, ...], ...]:
    return tuple(_bool_tuple(row, label) for row in _require_list(value, label))


def _unique_ids(values: Iterable[str], label: str) -> set[str]:
    items = tuple(values)
    if len(set(items)) != len(items):
        raise RagCheckerEvaluationError(f"{label} contains duplicates")
    return set(items)
