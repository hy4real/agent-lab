"""Chunk-size experiment over a fixed corpus with gold relevance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .evaluate import evaluate
from .index import LexicalIndex
from .chunking import chunk_text


@dataclass(frozen=True)
class ChunkingReport:
    documents: int
    queries: int
    k: int
    by_chunk_size: dict[int, dict[str, float]]

    def best_chunk_size(self) -> int:
        return max(self.by_chunk_size, key=lambda size: self.by_chunk_size[size]["recall_at_k"])


def run_chunking_experiment(
    *,
    documents: Sequence[str],
    queries: Sequence[tuple[str, str]],
    chunk_sizes: Sequence[int],
    overlap: int = 0,
    k: int = 3,
) -> ChunkingReport:
    if not documents or not queries or not chunk_sizes:
        raise ValueError("documents/queries/chunk_sizes 不能为空")
    by_size: dict[int, dict[str, float]] = {}
    for chunk_size in chunk_sizes:
        chunks: list[str] = []
        for doc_index, document in enumerate(documents):
            for chunk in chunk_text(document, chunk_size=chunk_size, overlap=overlap):
                chunks.append(chunk)
        index = LexicalIndex(chunks)
        retrievals = []
        for query, answer_phrase in queries:
            gold = {chunk_index for chunk_index, chunk in enumerate(chunks) if answer_phrase in chunk}
            retrieved = index.search(query, top_k=k)
            retrievals.append((retrieved, gold))
        by_size[chunk_size] = evaluate(retrievals, k=k)
    return ChunkingReport(documents=len(documents), queries=len(queries), k=k, by_chunk_size=by_size)


def render_experiment(report: ChunkingReport) -> str:
    lines = [f"chunking experiment: docs={report.documents} queries={report.queries} k={report.k}"]
    for size in sorted(report.by_chunk_size):
        metrics = report.by_chunk_size[size]
        lines.append(f"  chunk_size={size} hit@{report.k}={metrics['hit_at_k']:.3f} recall@{report.k}={metrics['recall_at_k']:.3f}")
    lines.append(f"  best_chunk_size={report.best_chunk_size()}")
    return "\n".join(lines)
