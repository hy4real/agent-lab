"""Retrieval experiment harness contract tests (offline, deterministic)."""

from __future__ import annotations

import pytest

from agentlab.demo import run_retrieval_demo
from agentlab.retrieval import (
    LexicalIndex,
    chunk_text,
    evaluate,
    hit_at_k,
    recall_at_k,
    render_experiment,
    run_chunking_experiment,
)


def test_chunk_text_sizes_and_overlap() -> None:
    text = "abcdefghij"
    assert chunk_text(text, chunk_size=4) == ("abcd", "efgh", "ij")
    assert chunk_text(text, chunk_size=4, overlap=2) == ("abcd", "cdef", "efgh", "ghij", "ij")
    with pytest.raises(ValueError):
        chunk_text(text, chunk_size=0)
    with pytest.raises(ValueError):
        chunk_text(text, chunk_size=4, overlap=4)


def test_lexical_index_search_order() -> None:
    index = LexicalIndex(("缓存与离线评测", "使用 Python 构建 agent", "无关文本"))
    assert index.search("缓存 评测", top_k=2) == (0,)
    assert index.search("python agent", top_k=2) == (1,)
    assert index.search("天气 摘要") == ()


def test_retrieval_metrics() -> None:
    assert hit_at_k([0, 1], {1}) is True
    assert hit_at_k([2, 3], {1}) is False
    assert recall_at_k([0, 1], {1, 2}) == 0.5
    metrics = evaluate((([0, 1], {1}), ([2], {3})), k=2)
    assert metrics["hit_at_k"] == 0.5
    assert metrics["recall_at_k"] == 0.5


def test_chunking_experiment_is_deterministic() -> None:
    documents = (
        "本文介绍缓存策略显著提升命中率并降低延迟，缓存采用 LRU 策略并做离线评测。",
        "本文介绍异步队列与任务调度，队列支持优先级并支持延迟执行。",
        "本文介绍熔断器与降级策略，用于高可用系统并支持故障注入测试。",
    )
    queries = (("缓存 命中率 离线评测", "缓存策略显著提升命中率并降低延迟"), ("异步 队列 调度", "异步队列与任务调度"))

    def run():
        return run_chunking_experiment(documents=documents, queries=queries, chunk_sizes=(6, 12, 24), k=3)

    first = run()
    second = run()
    assert first == second
    assert first.best_chunk_size() == 24
    assert first.by_chunk_size[6]["recall_at_k"] < first.by_chunk_size[24]["recall_at_k"]
    assert "best_chunk_size" in render_experiment(first)


def test_retrieval_demo_is_reproducible() -> None:
    assert run_retrieval_demo() == run_retrieval_demo()
    assert "best_chunk_size" in run_retrieval_demo()
