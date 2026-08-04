"""Resilience component contract tests (fully offline, deterministic)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from agentlab.demo import run_resilience_demo
from agentlab.resilience import (
    CircuitBreaker,
    DurableTaskQueue,
    RateLimiter,
    Task,
    TaskQueueError,
    TTLCache,
    WorkerPool,
    render_load_report,
    run_load_test,
)


def _clock() -> str:
    return "2026-07-31T00:00:00+00:00"


def _queue(tmp_path: Path) -> DurableTaskQueue:
    return DurableTaskQueue(state_path=tmp_path / "journal.jsonl", clock=_clock)


def test_queue_priority_and_insertion_order() -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="agentlab-queue-test."))
    queue = DurableTaskQueue(state_path=state_dir / "j.jsonl", clock=_clock)
    queue.add(Task(id="a", payload="low", priority=2))
    queue.add(Task(id="b", payload="high", priority=1))
    queue.add(Task(id="c", payload="same", priority=1))
    order = [task.id for task in queue.ready(_clock())]
    assert order == ["b", "c", "a"]
    for path in state_dir.glob("*"):
        path.unlink()
    state_dir.rmdir()


def test_queue_delay_scheduling() -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="agentlab-queue-test."))
    queue = DurableTaskQueue(state_path=state_dir / "j.jsonl", clock=_clock)
    queue.add(Task(id="later", payload="x", run_at="2026-07-31T00:00:10+00:00"))
    queue.add(Task(id="now", payload="y"))
    assert [t.id for t in queue.ready("2026-07-31T00:00:05+00:00")] == ["now"]
    assert [t.id for t in queue.ready("2026-07-31T00:00:11+00:00")] == ["now", "later"]
    for path in state_dir.glob("*"):
        path.unlink()
    state_dir.rmdir()


def test_queue_persistence_and_crash_recovery(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    first = DurableTaskQueue(state_path=path, clock=_clock)
    first.add(Task(id="a", payload="alpha"))
    first.add(Task(id="b", payload="beta"))
    first.ack("a")
    recovered = DurableTaskQueue(state_path=path, clock=_clock)
    assert recovered.pending_count() == 1
    assert [task.id for task in recovered.ready(_clock())] == ["b"]
    assert (path.stat().st_mode & 0o777) == 0o600


def test_queue_rejects_duplicates_and_corrupt_log(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.add(Task(id="a", payload="x"))
    with pytest.raises(TaskQueueError):
        queue.add(Task(id="a", payload="y"))
    path = tmp_path / "journal.jsonl"
    path.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(TaskQueueError):
        DurableTaskQueue(state_path=path, clock=_clock)


def test_worker_pool_drains_concurrently_with_counts(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    for index in range(5):
        queue.add(Task(id=f"t-{index}", payload=str(index)))
    processed: list[str] = []

    def processor(task: Task) -> str:
        processed.append(task.id)
        return "ok"

    stats = asyncio.run(WorkerPool(workers=2, processor=processor).drain(queue, now=_clock()))
    assert stats.total == 5 and stats.ok == 5 and stats.failed == 0
    assert sorted(processed) == [f"t-{index}" for index in range(5)]
    assert queue.pending_count() == 0


def test_worker_pool_counts_failures(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.add(Task(id="good", payload="ok"))
    queue.add(Task(id="bad", payload="boom"))

    def processor(task: Task) -> str:
        if task.id == "bad":
            raise RuntimeError("boom")
        return "ok"

    stats = asyncio.run(WorkerPool(workers=2, processor=processor).drain(queue, now=_clock()))
    assert stats.total == 2 and stats.ok == 1 and stats.failed == 1


def test_cache_lru_eviction_and_stats() -> None:
    times = iter(["2026-07-31T00:00:00+00:00"] * 10)
    cache = TTLCache(capacity=2, clock=lambda: next(times))
    cache.put("a", "1")
    cache.put("b", "2")
    assert cache.get("a") == "1"
    cache.put("c", "3")
    assert cache.get("b") is None
    assert cache.get("c") == "3"
    stats = cache.stats()
    assert stats["evictions"] == 1 and stats["hits"] == 2 and stats["misses"] == 1


def test_cache_ttl_expiry() -> None:
    clock_values = ["2026-07-31T00:00:00+00:00", "2026-07-31T00:00:05+00:00"]
    clock = iter(clock_values)
    cache = TTLCache(capacity=4, clock=lambda: next(clock), ttl_seconds=3)
    cache.put("k", "v")
    assert cache.get("k") is None


def test_rate_limiter_window() -> None:
    times = iter([0.0, 1.0, 2.0, 3.0, 12.0])
    limiter = RateLimiter(max_calls=2, window_seconds=10, clock=lambda: next(times))
    assert limiter.allow() and limiter.allow()
    assert not limiter.allow()
    assert not limiter.allow()
    assert limiter.allow()


def test_circuit_breaker_open_half_open_fallback() -> None:
    times = iter([0.0, 1.0, 2.0, 100.0, 100.0])
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=50, clock=lambda: next(times))

    def failing() -> str:
        raise RuntimeError("boom")

    assert breaker.state() == "closed"
    with pytest.raises(RuntimeError):
        breaker.call(failing)
    assert breaker.state() == "closed"
    with pytest.raises(RuntimeError):
        breaker.call(failing)
    assert breaker.state() == "open"
    assert breaker.call(failing, fallback=lambda: "fallback") == "fallback"
    assert breaker.state() == "half_open"


def test_load_test_counts_are_deterministic() -> None:
    def processor(task: Task) -> str:
        return "ok"

    report = run_load_test(task_count=50, worker_count=4, processor=processor, clock=_clock)
    assert report.task_count == 50 and report.ok == 50 and report.failed == 0
    assert "不作为实时并发能力结论" in render_load_report(report)


def test_resilience_demo_is_reproducible() -> None:
    assert run_resilience_demo() == run_resilience_demo()
    assert "stats" in run_resilience_demo()
