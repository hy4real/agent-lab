"""Retrieval quality metrics: hit@k and recall@k against gold documents."""

from __future__ import annotations

from typing import Sequence


def hit_at_k(retrieved_documents: Sequence[int], gold_documents: set[int]) -> bool:
    return bool(set(retrieved_documents) & gold_documents)


def recall_at_k(retrieved_documents: Sequence[int], gold_documents: set[int]) -> float:
    if not gold_documents:
        return 0.0
    return len(set(retrieved_documents) & gold_documents) / len(gold_documents)


def evaluate(retrievals: Sequence[tuple[Sequence[int], set[int]]], *, k: int = 3) -> dict[str, float]:
    if not retrievals:
        raise ValueError("至少需要一个查询")
    hits = sum(1 for retrieved, gold in retrievals if hit_at_k(retrieved[:k], gold))
    recall = sum(recall_at_k(retrieved[:k], gold) for retrieved, gold in retrievals) / len(retrievals)
    return {"queries": len(retrievals), "hit_at_k": hits / len(retrievals), "recall_at_k": recall}
