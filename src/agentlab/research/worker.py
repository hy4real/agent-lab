"""Lease-aware worker for offline, read-only ResearchAgent operations."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Callable

from .agent import ResearchAgent
from .corpus import Corpus
from .tasks import (
    EXECUTOR_VERSION, ResearchTask, ResearchTaskStore, TaskStoreError, validate_identifier, validate_lease,
)
from .task_events import emit_task_event

_MAX_RESULT_BYTES = 16 * 1024 * 1024


def _execute(
    corpus: Corpus, question: str, result_path: str, deadline: float,
    processor: Callable[[str], dict[str, Any]] | None,
) -> None:
    """Spawn target: no database connection and no raw output escapes this process."""
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)

    def guard() -> None:
        parent = multiprocessing.parent_process()
        while True:
            if time.monotonic() >= deadline or (parent is not None and not parent.is_alive()):
                os._exit(124)
            time.sleep(0.05)

    threading.Thread(target=guard, daemon=True, name="research-execution-guard").start()
    try:
        result = processor(question) if processor is not None else ResearchAgent(corpus).run(question).to_dict()
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not isinstance(result, dict) or len(serialized) > _MAX_RESULT_BYTES:
            raise ValueError("invalid_result")
        # Parent only reads after successful process exit, so no partial result is accepted.
        fd = os.open(result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(serialized)
    except Exception:
        raise SystemExit(1) from None


def _reap(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        process.kill()
    process.join(timeout=1)
    if process.is_alive():
        raise TaskStoreError("execution_process_unresponsive")
    process.close()


@dataclass(frozen=True)
class WorkerOutcome:
    task_id: str | None
    status: str
    attempt: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "status": self.status, "attempt": self.attempt}


class ResearchWorker:
    def __init__(
        self, store: ResearchTaskStore, corpus: Corpus, *, worker_id: str,
        lease_seconds: float = 30.0,
        processor: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        validate_identifier(worker_id)
        validate_lease(lease_seconds)
        self.store = store
        self.corpus = corpus
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        # Optional injected processor must be spawn-picklable, just like the default target.
        self.processor = processor

    def run_once(self) -> WorkerOutcome:
        started = time.monotonic()
        try:
            outcome = self._run_once()
        except KeyboardInterrupt:
            emit_task_event("worker_error", status="interrupted", duration_ms=round((time.monotonic() - started) * 1000))
            raise
        except TaskStoreError:
            emit_task_event("worker_error", error_code="task_store_unavailable")
            raise
        if outcome.task_id is not None:
            code = None
            try:
                current = self.store.get(outcome.task_id)
                if current is not None and current.attempt == outcome.attempt:
                    code = current.error_code
            except TaskStoreError:
                code = "task_store_unavailable"
            emit_task_event(
                "worker_finished", task_id=outcome.task_id, attempt=outcome.attempt,
                status=outcome.status, error_code=code,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        return outcome

    def _run_once(self) -> WorkerOutcome:
        task = self.store.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if task is None:
            return WorkerOutcome(None, "idle", None)
        emit_task_event(
            "worker_claimed", task_id=task.id, attempt=task.attempt, status="running",
            queue_wait_ms=max(0, round((self.store.clock() - task.created_at) * 1000)),
        )
        mismatch = None
        if task.corpus_hash != self.corpus.content_hash:
            mismatch = "corpus_mismatch"
        elif task.payload["executor_version"] != EXECUTOR_VERSION:
            mismatch = "executor_mismatch"
        if mismatch is not None:
            accepted = self.store.fail(task, mismatch)
            return WorkerOutcome(task.id, "failed" if accepted else "lost_lease", task.attempt)

        remaining = max(0.0, task.deadline_at - self.store.clock())
        deadline = time.monotonic() + remaining
        with tempfile.TemporaryDirectory(prefix="agentlab-execution-") as directory:
            result_path = Path(directory) / "result.json"
            process = multiprocessing.get_context("spawn").Process(
                target=_execute,
                args=(self.corpus, task.payload["question"], str(result_path), deadline, self.processor),
            )
            process.start()
            try:
                next_renewal = time.monotonic() + self.lease_seconds / 3
                while True:
                    current = self.store.get(task.id)
                    if (current is None or current.status != "running"
                            or current.worker_id != task.worker_id or current.attempt != task.attempt
                            or current.lease_until is None or current.lease_until <= self.store.clock()):
                        return self._lost(task, current)
                    if time.monotonic() >= deadline:
                        self.store.fail(task, "deadline_exceeded")
                        return self._lost(task, self.store.get(task.id))
                    if time.monotonic() >= next_renewal:
                        if not self.store.renew(task, lease_seconds=self.lease_seconds):
                            return self._lost(task, self.store.get(task.id))
                        next_renewal = time.monotonic() + self.lease_seconds / 3
                    if not process.is_alive():
                        process.join()
                        break
                    process.join(timeout=min(0.1, self.lease_seconds / 3, max(0, deadline - time.monotonic())))
                if process.exitcode != 0:
                    accepted = self.store.fail(task, "research_failed")
                    return self._finished(task, "failed", accepted)
                try:
                    if result_path.stat().st_size > _MAX_RESULT_BYTES:
                        raise ValueError("invalid_result")
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if not isinstance(result, dict):
                        raise ValueError("invalid_result")
                except (OSError, ValueError):
                    accepted = self.store.fail(task, "research_failed")
                    return self._finished(task, "failed", accepted)
                if time.monotonic() >= deadline:
                    self.store.fail(task, "deadline_exceeded")
                    return self._lost(task, self.store.get(task.id))
                accepted = self.store.succeed(task, result)
                return self._finished(task, "succeeded", accepted)
            except TaskStoreError:
                return WorkerOutcome(task.id, "lost_lease", task.attempt)
            finally:
                _reap(process)

    @staticmethod
    def _lost(task: ResearchTask, current: ResearchTask | None) -> WorkerOutcome:
        reason = current.error_code if current is not None else None
        status = reason if reason in {"cancelled", "deadline_exceeded"} else "lost_lease"
        return WorkerOutcome(task.id, status, task.attempt)

    def _finished(self, task: ResearchTask, status: str, accepted: bool) -> WorkerOutcome:
        if accepted:
            return WorkerOutcome(task.id, status, task.attempt)
        return self._lost(task, self.store.get(task.id))
