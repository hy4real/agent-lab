"""Unit tests for the paper-derived RAGChecker metric kernel."""

from __future__ import annotations

import pytest

from agentlab.research.ragchecker_metrics import (
    ClaimEntailmentMatrices,
    RagCheckerMetricError,
    RagCheckerMetrics,
    compute_ragchecker_metrics,
    metrics_match,
)


def _mixed_case() -> ClaimEntailmentMatrices:
    return ClaimEntailmentMatrices(
        ground_truth_claim_ids=("gt-a", "gt-b", "gt-c"),
        response_claim_ids=("r-correct", "r-relevant", "r-irrelevant", "r-hallucination", "r-self"),
        retrieved_chunk_ids=("chunk-relevant", "chunk-irrelevant"),
        answer2response=(True, False, False, False, True),
        response2answer=(True, True, False),
        retrieved2answer=((True, False), (False, False), (False, False)),
        retrieved2response=(
            (True, False),
            (True, False),
            (False, True),
            (False, False),
            (False, False),
        ),
    )


def test_nonsquare_case_reproduces_all_paper_metrics() -> None:
    metrics = compute_ragchecker_metrics(_mixed_case())
    expected = RagCheckerMetrics(
        precision=0.4,
        recall=2 / 3,
        f1=0.5,
        claim_recall=1 / 3,
        context_precision=0.5,
        context_utilization=1.0,
        noise_sensitivity_in_relevant=0.2,
        noise_sensitivity_in_irrelevant=0.2,
        hallucination=0.2,
        self_knowledge=0.2,
        faithfulness=0.6,
    )
    assert metrics_match(metrics, expected)


def test_overlap_is_counted_only_as_relevant_noise() -> None:
    matrices = ClaimEntailmentMatrices(
        ground_truth_claim_ids=("gt",),
        response_claim_ids=("incorrect",),
        retrieved_chunk_ids=("relevant", "irrelevant"),
        answer2response=(False,),
        response2answer=(False,),
        retrieved2answer=((True, False),),
        retrieved2response=((True, True),),
    )
    metrics = compute_ragchecker_metrics(matrices)
    assert metrics.noise_sensitivity_in_relevant == 1.0
    assert metrics.noise_sensitivity_in_irrelevant == 0.0


@pytest.mark.parametrize(
    ("matrices", "expected"),
    [
        (
            ClaimEntailmentMatrices(
                ground_truth_claim_ids=("gt",),
                response_claim_ids=(),
                retrieved_chunk_ids=("chunk",),
                answer2response=(),
                response2answer=(False,),
                retrieved2answer=((True,),),
                retrieved2response=(),
            ),
            {"claim_recall": 1.0, "context_precision": 1.0, "faithfulness": 0.0},
        ),
        (
            ClaimEntailmentMatrices(
                ground_truth_claim_ids=("gt",),
                response_claim_ids=("response",),
                retrieved_chunk_ids=(),
                answer2response=(False,),
                response2answer=(False,),
                retrieved2answer=((),),
                retrieved2response=((),),
            ),
            {"hallucination": 0.0, "self_knowledge": 0.0, "faithfulness": 0.0},
        ),
        (
            ClaimEntailmentMatrices(
                ground_truth_claim_ids=(),
                response_claim_ids=("response",),
                retrieved_chunk_ids=("chunk",),
                answer2response=(False,),
                response2answer=(),
                retrieved2answer=(),
                retrieved2response=((True,),),
            ),
            {"claim_recall": 0.0, "context_precision": 0.0, "faithfulness": 1.0},
        ),
    ],
)
def test_degenerate_inputs_match_official_implementation_behavior(
    matrices: ClaimEntailmentMatrices,
    expected: dict[str, float],
) -> None:
    values = compute_ragchecker_metrics(matrices).to_dict()
    assert {name: values[name] for name in expected} == expected


def test_correct_unretrieved_claim_is_self_knowledge() -> None:
    matrices = ClaimEntailmentMatrices(
        ground_truth_claim_ids=("gt",),
        response_claim_ids=("response",),
        retrieved_chunk_ids=("chunk",),
        answer2response=(True,),
        response2answer=(True,),
        retrieved2answer=((False,),),
        retrieved2response=((False,),),
    )
    metrics = compute_ragchecker_metrics(matrices)
    assert metrics.self_knowledge == 1.0
    assert metrics.hallucination == 0.0
    assert metrics.faithfulness == 0.0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"ground_truth_claim_ids": ("gt", "gt")}, "duplicates"),
        ({"answer2response": (1, False, False, False, True)}, "bool values"),
        ({"response2answer": (True, False)}, "shape"),
        ({"retrieved2answer": ((True, False, True),) * 3}, "shape"),
        ({"retrieved2response": ((True,),) * 5}, "shape"),
    ],
)
def test_contract_rejects_ambiguous_or_malformed_inputs(
    changes: dict[str, object],
    message: str,
) -> None:
    values = _mixed_case().__dict__.copy()
    values.update(changes)
    with pytest.raises(RagCheckerMetricError, match=message):
        ClaimEntailmentMatrices(**values)


def test_expected_metric_schema_is_strict() -> None:
    expected = compute_ragchecker_metrics(_mixed_case()).to_dict()
    assert RagCheckerMetrics.from_mapping(expected).to_dict() == expected
    with pytest.raises(RagCheckerMetricError, match="fields"):
        RagCheckerMetrics.from_mapping({"precision": 1.0})
    with pytest.raises(RagCheckerMetricError, match="value"):
        RagCheckerMetrics.from_mapping({**expected, "precision": True})
    with pytest.raises(RagCheckerMetricError, match="range"):
        RagCheckerMetrics.from_mapping({**expected, "precision": 1.1})


def test_metric_comparison_rejects_negative_tolerance() -> None:
    metrics = compute_ragchecker_metrics(_mixed_case())
    with pytest.raises(ValueError, match="non-negative"):
        metrics_match(metrics, metrics, tolerance=-1)
