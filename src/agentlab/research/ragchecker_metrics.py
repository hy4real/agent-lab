"""Deterministic claim-level metrics reproduced from RAGChecker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


METRIC_NAMES = (
    "precision",
    "recall",
    "f1",
    "claim_recall",
    "context_precision",
    "context_utilization",
    "noise_sensitivity_in_relevant",
    "noise_sensitivity_in_irrelevant",
    "hallucination",
    "self_knowledge",
    "faithfulness",
)


class RagCheckerMetricError(ValueError):
    """Stable validation error for oracle-label metric inputs."""


@dataclass(frozen=True)
class ClaimEntailmentMatrices:
    ground_truth_claim_ids: tuple[str, ...]
    response_claim_ids: tuple[str, ...]
    retrieved_chunk_ids: tuple[str, ...]
    answer2response: tuple[bool, ...]
    response2answer: tuple[bool, ...]
    retrieved2answer: tuple[tuple[bool, ...], ...]
    retrieved2response: tuple[tuple[bool, ...], ...]

    def __post_init__(self) -> None:
        _validate_ids(self.ground_truth_claim_ids, "ground_truth_claim_ids")
        _validate_ids(self.response_claim_ids, "response_claim_ids")
        _validate_ids(self.retrieved_chunk_ids, "retrieved_chunk_ids")
        _validate_vector(self.answer2response, len(self.response_claim_ids), "answer2response")
        _validate_vector(self.response2answer, len(self.ground_truth_claim_ids), "response2answer")
        _validate_matrix(
            self.retrieved2answer,
            len(self.ground_truth_claim_ids),
            len(self.retrieved_chunk_ids),
            "retrieved2answer",
        )
        _validate_matrix(
            self.retrieved2response,
            len(self.response_claim_ids),
            len(self.retrieved_chunk_ids),
            "retrieved2response",
        )


@dataclass(frozen=True)
class RagCheckerMetrics:
    precision: float
    recall: float
    f1: float
    claim_recall: float
    context_precision: float
    context_utilization: float
    noise_sensitivity_in_relevant: float
    noise_sensitivity_in_irrelevant: float
    hallucination: float
    self_knowledge: float
    faithfulness: float

    def to_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in METRIC_NAMES}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RagCheckerMetrics":
        if set(value) != set(METRIC_NAMES):
            raise RagCheckerMetricError("metric fields invalid")
        metrics: dict[str, float] = {}
        for name in METRIC_NAMES:
            raw = value[name]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise RagCheckerMetricError("metric value invalid")
            number = float(raw)
            if not 0.0 <= number <= 1.0:
                raise RagCheckerMetricError("metric value out of range")
            metrics[name] = number
        return cls(**metrics)


def compute_ragchecker_metrics(matrices: ClaimEntailmentMatrices) -> RagCheckerMetrics:
    precision = _mean(matrices.answer2response)
    recall = _mean(matrices.response2answer)
    f1 = 2 * precision * recall / (precision + recall) if precision > 0 and recall > 0 else 0.0

    ground_truth_count = len(matrices.ground_truth_claim_ids)
    response_count = len(matrices.response_claim_ids)
    chunk_count = len(matrices.retrieved_chunk_ids)

    claim_recall = 0.0
    context_precision = 0.0
    context_utilization = 0.0
    recalled_claims: tuple[bool, ...] = ()
    relevant_chunks: tuple[bool, ...] = ()
    if ground_truth_count > 0 and chunk_count > 0:
        recalled_claims = tuple(any(row) for row in matrices.retrieved2answer)
        relevant_chunks = tuple(
            any(matrices.retrieved2answer[claim_index][chunk_index] for claim_index in range(ground_truth_count))
            for chunk_index in range(chunk_count)
        )
        claim_recall = _mean(recalled_claims)
        context_precision = _mean(relevant_chunks)
        recalled_count = sum(recalled_claims)
        if recalled_count:
            used_count = sum(
                recalled and matrices.response2answer[index]
                for index, recalled in enumerate(recalled_claims)
            )
            context_utilization = used_count / recalled_count

    relevant_noise = 0.0
    irrelevant_noise = 0.0
    if response_count > 0 and chunk_count > 0 and ground_truth_count > 0:
        relevant_errors: list[bool] = []
        irrelevant_errors: list[bool] = []
        for index, row in enumerate(matrices.retrieved2response):
            supported_by_relevant = any(
                relevant_chunks[chunk_index] and row[chunk_index]
                for chunk_index in range(chunk_count)
            )
            supported_by_irrelevant = any(
                not relevant_chunks[chunk_index] and row[chunk_index]
                for chunk_index in range(chunk_count)
            )
            incorrect = not matrices.answer2response[index]
            relevant_errors.append(supported_by_relevant and incorrect)
            irrelevant_errors.append(
                supported_by_irrelevant and not supported_by_relevant and incorrect
            )
        relevant_noise = _mean(relevant_errors)
        irrelevant_noise = _mean(irrelevant_errors)

    hallucination = 0.0
    self_knowledge = 0.0
    faithfulness = 0.0
    if response_count > 0 and chunk_count > 0:
        supported = tuple(any(row) for row in matrices.retrieved2response)
        hallucination = _mean(
            tuple(not supported[index] and not matrices.answer2response[index] for index in range(response_count))
        )
        self_knowledge = _mean(
            tuple(not supported[index] and matrices.answer2response[index] for index in range(response_count))
        )
        faithfulness = _mean(supported)

    return RagCheckerMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        claim_recall=claim_recall,
        context_precision=context_precision,
        context_utilization=context_utilization,
        noise_sensitivity_in_relevant=relevant_noise,
        noise_sensitivity_in_irrelevant=irrelevant_noise,
        hallucination=hallucination,
        self_knowledge=self_knowledge,
        faithfulness=faithfulness,
    )


def metrics_match(
    actual: RagCheckerMetrics,
    expected: RagCheckerMetrics,
    *,
    tolerance: float = 1e-12,
) -> bool:
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    return all(
        abs(actual.to_dict()[name] - expected.to_dict()[name]) <= tolerance
        for name in METRIC_NAMES
    )


def _mean(values: Sequence[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def _validate_ids(values: tuple[str, ...], label: str) -> None:
    if not isinstance(values, tuple) or any(type(value) is not str or not value for value in values):
        raise RagCheckerMetricError(f"{label} invalid")
    if len(set(values)) != len(values):
        raise RagCheckerMetricError(f"{label} contains duplicates")


def _validate_vector(values: tuple[bool, ...], size: int, label: str) -> None:
    if not isinstance(values, tuple) or len(values) != size:
        raise RagCheckerMetricError(f"{label} shape invalid")
    if any(type(value) is not bool for value in values):
        raise RagCheckerMetricError(f"{label} must contain bool values")


def _validate_matrix(
    rows: tuple[tuple[bool, ...], ...],
    row_count: int,
    column_count: int,
    label: str,
) -> None:
    if not isinstance(rows, tuple) or len(rows) != row_count:
        raise RagCheckerMetricError(f"{label} shape invalid")
    for row in rows:
        if not isinstance(row, tuple) or len(row) != column_count:
            raise RagCheckerMetricError(f"{label} shape invalid")
        if any(type(value) is not bool for value in row):
            raise RagCheckerMetricError(f"{label} must contain bool values")
