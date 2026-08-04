"""Frozen RAG suite tests without a model or network."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentlab.research.corpus import SearchHit
from agentlab.research.rag_evaluation import (
    RagEvaluationError,
    load_rag_fixture,
    run_rag_evaluation,
)
from agentlab.research.retrieval import HybridRetriever


ROOT = Path(__file__).parents[1] / "evals" / "research-rag-v2"
CANONICAL_REPORT = ROOT / "canonical-report.json"


def _provenance(version: str) -> dict[str, str | int]:
    return {"index_version": version, "embedding_backend": "synthetic"}


class GoldVectorSearch:
    def __init__(self, corpus, cases) -> None:
        self.corpus = corpus
        self.gold = {case.question: case.gold_sources for case in cases}

    def search(self, query, *, top_k=5):
        names = self.gold[query]
        sources = {source.name: source for source in self.corpus.sources}
        chunks = {chunk.source_id: chunk for chunk in self.corpus.chunks}
        ordered = [sources[name] for name in names]
        ordered.extend(source for source in self.corpus.sources if source.name not in names)
        return tuple(
            SearchHit(
                chunks[source.id].id,
                source.id,
                source.name,
                1.0 - index * 0.01,
            )
            for index, source in enumerate(ordered[:top_k])
        )


def test_frozen_rag_suite_meets_pre_registered_contracts() -> None:
    cases, corpus = load_rag_fixture(ROOT)
    hybrid = HybridRetriever(corpus, GoldVectorSearch(corpus, cases))
    report = run_rag_evaluation(
        cases,
        corpus,
        hybrid,
        index_version="b" * 64,
        index_provenance=_provenance("b" * 64),
    )
    assert report.passed is True
    assert report.result == "passed"
    assert report.metrics["hybrid_semantic_recall_at_5"] == 1.0
    assert report.metrics["semantic_recall_absolute_gain"] >= 0.20
    assert report.metrics["citation_validity"] == 1.0
    assert report.metrics["unsupported_factual_claims"] == 0
    assert report.metrics["refusal_contract"] == 1.0
    assert report.metrics["prompt_injection_contract"] == 1.0
    assert report.metrics["hybrid_mainline_contract"] == 1.0


def test_rag_report_omits_questions_and_corpus_text() -> None:
    cases, corpus = load_rag_fixture(ROOT)
    report = run_rag_evaluation(
        cases,
        corpus,
        HybridRetriever(corpus, GoldVectorSearch(corpus, cases)),
        index_version="c" * 64,
        index_provenance=_provenance("c" * 64),
    )
    serialized = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "How should a car" not in serialized
    assert "Ignore system rules" not in serialized
    assert str(ROOT) not in serialized


def test_rag_suite_and_recursive_corpus_are_frozen(tmp_path: Path) -> None:
    copied = tmp_path / "research-rag-v2"
    shutil.copytree(ROOT, copied)
    suite = copied / "suite-v2.json"
    suite.write_text(suite.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RagEvaluationError, match="必须提升版本"):
        load_rag_fixture(copied)

    shutil.copytree(ROOT, copied, dirs_exist_ok=True)
    nested = copied / "corpus" / "nested"
    nested.mkdir()
    (nested / "extra.md").write_text("synthetic extra", encoding="utf-8")
    with pytest.raises(RagEvaluationError, match="必须提升版本"):
        load_rag_fixture(copied)


def test_canonical_report_is_public_safe_and_matches_frozen_contract() -> None:
    value = json.loads(CANONICAL_REPORT.read_text(encoding="utf-8"))

    assert value["suite_id"] == "research-rag-suite-v2"
    assert value["result"] == "passed"
    assert value["passed"] is True
    assert value["metrics"]["hybrid_semantic_recall_at_5"] == 1.0
    assert value["metrics"]["lexical_semantic_recall_at_5"] == 0.5
    assert value["metrics"]["unsupported_factual_claims"] == 0
    assert len(value["cases"]) == 13
    assert value["index_provenance"]["point_count"] == 12
    assert value["index_provenance"]["index_version"] == value["index_version"]

    serialized = json.dumps(value, ensure_ascii=False)
    suite = (ROOT / "suite-v2.json").read_text(encoding="utf-8")
    assert all(case["question"] not in serialized for case in json.loads(suite)["cases"])
    assert str(ROOT) not in serialized
    assert "Provider quality" in value["boundary"]
