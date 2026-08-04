"""Deterministic hybrid retrieval tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentlab.research.corpus import Corpus
from agentlab.research.retrieval import HybridRetriever


class KeywordEmbeddings:
    def embed(self, texts):
        return tuple(
            (
                1.0 if "automobile" in text.lower() or "car" in text.lower() else 0.0,
                1.0 if "weather" in text.lower() else 0.0,
            )
            for text in texts
        )


def test_hybrid_retrieval_adds_semantic_rank(tmp_path: Path) -> None:
    (tmp_path / "transport.md").write_text("An automobile needs regular service.", encoding="utf-8")
    (tmp_path / "weather.md").write_text("Weather is sunny.", encoding="utf-8")
    corpus = Corpus.load(tmp_path)
    hits = HybridRetriever(corpus, KeywordEmbeddings()).search("car maintenance", top_k=2)
    assert hits[0].source_name == "transport.md"


def test_hybrid_retrieval_rejects_bad_dimensions(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    corpus = Corpus.load(tmp_path)

    class BadEmbeddings:
        def embed(self, texts):
            return ((1.0,), (2.0,))

    with pytest.raises(ValueError, match="数量"):
        HybridRetriever(corpus, BadEmbeddings())
