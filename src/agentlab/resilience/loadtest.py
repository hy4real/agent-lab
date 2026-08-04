"""Bounded offline load test over the durable queue (diagnostic only)."""

from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .queue import DurableTaskQueue, Task, WorkerPool


@dataclass(frozen=True)
class LoadReport:
    task_count: int
    ok: int
    failed: int
    worker_count: int
    duration_seconds: float


def run_load_test(
    *,
    task_count: int,
    worker_count: int,
    processor: Callable[[Task], str],
    clock: Callable[[], str],
) -> LoadReport:
    """Push fixed tasks and drain concurrently; counts are deterministic."""
    if task_count <= 0 or worker_count <= 0:
        raise ValueError("task_count 与 worker_count 必须为正整数")
    state_dir = Path(tempfile.mkdtemp(prefix="agentlab-loadtest."))
    try:
        queue = DurableTaskQueue(state_path=state_dir / "journal.jsonl", clock=clock)
        for index in range(task_count):
            queue.add(Task(id=f"task-{index}", payload=f"payload-{index}"))
        pool = WorkerPool(workers=worker_count, processor=processor)
        started = time.monotonic()
        stats = asyncio.run(pool.drain(queue, now=clock()))
        duration = time.monotonic() - started
        return LoadReport(
            task_count=task_count, ok=stats.ok, failed=stats.failed,
            worker_count=worker_count, duration_seconds=duration,
        )
    finally:
        for path in state_dir.glob("*"):
            path.unlink()
        state_dir.rmdir()


def render_load_report(report: LoadReport) -> str:
    return (
        "LoadReport(boundary: 不作为实时并发能力结论)\n"
        f"  tasks={report.task_count} ok={report.ok} failed={report.failed} "
        f"workers={report.worker_count} duration={report.duration_seconds:.3f}s"
    )
