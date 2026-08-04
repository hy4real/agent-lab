"""Reciprocal-rank fusion over lexical and vector retrieval."""

from __future__ import annotations

import math
from typing import Protocol, Sequence, cast

from .corpus import Corpus, SearchHit


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        ...


class VectorSearchProvider(Protocol):
    def search(self, query: str, *, top_k: int = 5) -> tuple[SearchHit, ...]:
        ...


class HybridRetriever:
    """Fuse lexical and vector ranks without treating rank score as confidence."""

    def __init__(
        self,
        corpus: Corpus,
        vector_search: VectorSearchProvider | EmbeddingProvider,
        *,
        rrf_k: int = 60,
    ) -> None:
        if rrf_k <= 0:
            raise ValueError("rrf_k 必须为正整数")
        self.corpus = corpus
        self.rrf_k = rrf_k
        self.vector_search = (
            cast(VectorSearchProvider, vector_search)
            if callable(getattr(vector_search, "search", None))
            else _EmbeddingVectorSearch(corpus, cast(EmbeddingProvider, vector_search))
        )

    def search(self, query: str, *, top_k: int = 5) -> tuple[SearchHit, ...]:
        if top_k <= 0 or top_k > 10:
            raise ValueError("top_k 必须在 1..10")
        candidate_limit = min(10, len(self.corpus.chunks))
        lexical = self.corpus.search(query, top_k=candidate_limit)
        semantic = self.vector_search.search(query, top_k=candidate_limit)
        scores: dict[str, float] = {}
        for rank, hit in enumerate(lexical, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (self.rrf_k + rank)
        for rank, hit in enumerate(semantic, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (self.rrf_k + rank)
        chunk_order = {chunk.id: index for index, chunk in enumerate(self.corpus.chunks)}
        ranked_ids = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_order[chunk_id]))
        chunks = {chunk.id: chunk for chunk in self.corpus.chunks}
        source_names = {source.id: source.name for source in self.corpus.sources}
        return tuple(
            SearchHit(
                chunk_id,
                chunks[chunk_id].source_id,
                source_names[chunks[chunk_id].source_id],
                scores[chunk_id],
            )
            for chunk_id in ranked_ids[:top_k]
        )


class _EmbeddingVectorSearch:
    """Compatibility adapter for deterministic injected-vector tests."""

    def __init__(self, corpus: Corpus, embeddings: EmbeddingProvider) -> None:
        self.corpus = corpus
        self.embeddings = embeddings
        self._chunk_vectors = embeddings.embed([chunk.text for chunk in corpus.chunks])
        if len(self._chunk_vectors) != len(corpus.chunks):
            raise ValueError("embedding 数量与 chunks 不一致")
        dimensions = {len(vector) for vector in self._chunk_vectors}
        if len(dimensions) != 1 or not dimensions or 0 in dimensions:
            raise ValueError("embedding 维度无效")

    def search(self, query: str, *, top_k: int = 5) -> tuple[SearchHit, ...]:
        if top_k <= 0 or top_k > 10:
            raise ValueError("top_k 必须在 1..10")
        query_vectors = self.embeddings.embed([query])
        if len(query_vectors) != 1:
            raise ValueError("query embedding 数量无效")
        query_vector = query_vectors[0]
        if len(query_vector) != len(self._chunk_vectors[0]):
            raise ValueError("query embedding 维度不一致")

        semantic = sorted(
            (
                (_cosine(query_vector, vector), index, chunk)
                for index, (chunk, vector) in enumerate(zip(self.corpus.chunks, self._chunk_vectors))
            ),
            key=lambda item: (-item[0], item[1]),
        )
        source_names = {source.id: source.name for source in self.corpus.sources}
        return tuple(
            SearchHit(chunk.id, chunk.source_id, source_names[chunk.source_id], similarity)
            for similarity, _index, chunk in semantic
            if similarity > 0
        )[:top_k]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
