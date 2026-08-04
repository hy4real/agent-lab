"""Durable task queue with priority/delay scheduling and crash recovery."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


class TaskQueueError(RuntimeError):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    payload: str
    priority: int = 0
    run_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise TaskQueueError("任务 id 无效")
        if not isinstance(self.payload, str) or not self.payload.strip():
            raise TaskQueueError("任务 payload 无效")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise TaskQueueError("任务 priority 必须是非负整数")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "payload": self.payload, "priority": self.priority, "run_at": self.run_at}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Task":
        return cls(
            id=str(value["id"]), payload=str(value["payload"]),
            priority=int(value.get("priority", 0)), run_at=value.get("run_at"),
        )


class DurableTaskQueue:
    """Append-only journaled queue; pending = adds - acks, replayed on init.

    Scheduling is deterministic: ready tasks order by (run_at, priority, insertion).
    This is a single-process queue; no multi-process/multi-host claim.
    """

    def __init__(self, *, state_path: Path, clock: Callable[[], str]) -> None:
        self.state_path = Path(state_path)
        self.clock = clock
        self._pending: dict[str, Task] = {}
        self._insertion: dict[str, int] = {}
        self._seq = 0
        self._recover()

    def add(self, task: Task) -> Task:
        if task.id in self._pending:
            raise TaskQueueError("重复任务 id")
        self._append({"op": "add", "task": task.to_dict()})
        self._pending[task.id] = task
        self._insertion[task.id] = self._seq
        self._seq += 1
        return task

    def ready(self, now: str) -> tuple[Task, ...]:
        items = [task for task in self._pending.values() if task.run_at is None or task.run_at <= now]
        return tuple(sorted(items, key=lambda task: (task.run_at or "", task.priority, self._insertion[task.id])))

    def ack(self, task_id: str) -> None:
        if task_id not in self._pending:
            raise TaskQueueError("未知任务")
        self._append({"op": "ack", "id": task_id})
        del self._pending[task_id]
        del self._insertion[task_id]

    def pending_count(self) -> int:
        return len(self._pending)

    def _append(self, record: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_path.parent, 0o700)
        with self.state_path.open("ab") as handle:
            handle.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.state_path, 0o600)

    def _recover(self) -> None:
        if not self.state_path.exists():
            return
        try:
            for line in self.state_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("op") == "add":
                    task = Task.from_dict(record["task"])
                    self._pending[task.id] = task
                    self._insertion[task.id] = self._seq
                    self._seq += 1
                elif record.get("op") == "ack":
                    self._pending.pop(record.get("id"), None)
                    self._insertion.pop(record.get("id"), None)
                else:
                    raise ValueError("未知日志记录")
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            raise TaskQueueError("任务队列日志损坏，拒绝继续使用") from exc


@dataclass(frozen=True)
class WorkerStats:
    total: int
    ok: int
    failed: int


class WorkerPool:
    """Process ready tasks concurrently; failures are acked and counted.

    Retry/redelivery is intentionally out of scope here (handled by the circuit
    breaker layer); this keeps the worker loop deterministic.
    """

    def __init__(self, *, workers: int, processor: Callable[[Task], str]) -> None:
        if workers <= 0:
            raise ValueError("workers 必须为正整数")
        self.workers = workers
        self.processor = processor

    async def drain(self, queue: DurableTaskQueue, *, now: str, max_tasks: int | None = None) -> WorkerStats:
        total = 0
        ok = 0
        failed = 0

        async def run(task: Task) -> bool:
            try:
                self.processor(task)
                queue.ack(task.id)
                return True
            except Exception:
                queue.ack(task.id)
                return False

        while True:
            ready = queue.ready(now)
            if not ready:
                break
            batch = ready[: self.workers] if max_tasks is None else ready[: min(self.workers, max_tasks - total)]
            if not batch:
                break
            results = await asyncio.gather(*(run(task) for task in batch))
            total += len(results)
            ok += sum(results)
            failed += len(results) - sum(results)
        return WorkerStats(total=total, ok=ok, failed=failed)
