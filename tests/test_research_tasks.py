"""Real SQLite contracts, independent processes and crash-window recovery."""

from __future__ import annotations

import json
import fcntl
import multiprocessing
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest

from agentlab.research.corpus import Corpus
from agentlab.research.tasks import (
    MAX_OUTSTANDING_TASKS, TASK_TIMEOUT_SECONDS,
    ResearchTaskStore, TaskCapacityExceeded, TaskConflict, TaskStoreError,
)
from agentlab.research.worker import ResearchWorker
from agentlab.research.task_events import emit_task_event


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "fact.md").write_text("The Helix fixture status is ready.", encoding="utf-8")
    return Corpus.load(root)


def test_commit_idempotency_restart_and_private_storage(tmp_path: Path, corpus: Corpus) -> None:
    path = tmp_path / "state" / "tasks.sqlite3"
    store = ResearchTaskStore(path)
    task, created = store.submit("request-1", "What is the Helix fixture status?", corpus.content_hash)
    assert created and task.status == "pending"
    restored, created = ResearchTaskStore(path).submit("request-1", task.payload["question"], corpus.content_hash)
    assert not created and restored == task
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(TaskConflict):
        store.submit("request-1", "different question", corpus.content_hash)
    with pytest.raises(TaskConflict):
        store.submit("request-1", task.payload["question"], "a" * 64)
    assert store.get("unknown") is None
    assert "question" not in json.dumps(task.public_dict())


def _parallel_action(path: str, action: str, barrier, output) -> None:
    try:
        barrier.wait(timeout=10)
        store = ResearchTaskStore(Path(path), clock=lambda: 100.0)
        barrier.wait(timeout=10)
        if action == "submit":
            task, created = store.submit("same-key", "same question", "a" * 64)
            output.put((task.id, created))
        elif action == "capacity":
            try:
                task, created = store.submit(f"unique-{os.getpid()}", "q", "a" * 64)
                output.put((task.id, created))
            except TaskCapacityExceeded:
                output.put(("full", False))
        else:
            task = store.claim(f"worker-{os.getpid()}")
            output.put(None if task is None else (task.id, task.attempt))
    except Exception as exc:
        output.put(("error", type(exc).__name__, str(exc), str(exc.__context__)))
        barrier.abort()


def _race(path: Path, action: str) -> list:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    output = context.Queue()
    processes = [context.Process(target=_parallel_action, args=(str(path), action, barrier, output)) for _ in range(4)]
    try:
        for process in processes:
            process.start()
        results = [output.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        assert all(item is None or item[0] != "error" for item in results), results
        return results
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output.close()


@pytest.mark.parametrize("startup_run", range(5))
def test_independent_processes_submit_once_and_claim_once(tmp_path: Path, startup_run: int) -> None:
    path = tmp_path / "state" / "tasks.sqlite3"
    submitted = _race(path, "submit")
    assert len({item[0] for item in submitted}) == 1
    assert sum(item[1] for item in submitted) == 1
    claimed = [item for item in _race(path, "claim") if item is not None]
    assert claimed == [(submitted[0][0], 1)]


def test_initialization_lock_wait_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    store = ResearchTaskStore(path)
    lock = path.with_name(path.name + ".init.lock")
    with lock.open("r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(TaskStoreError, match="task_store_unavailable"):
            ResearchTaskStore(path, busy_timeout=0.02)
    assert ResearchTaskStore(path).submit("req", "q", "a" * 64)[1]
    assert store.get("missing") is None


def test_lease_expiry_takeover_and_same_worker_id_aba(tmp_path: Path) -> None:
    now = [100.0]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    original, _ = store.submit("req", "question", "a" * 64)
    a = store.claim("same-worker", lease_seconds=10)
    assert a is not None
    now[0] = 109.0
    assert store.renew(a, lease_seconds=10)
    now[0] = 110.0
    assert store.claim("worker-b") is None
    now[0] = 119.0
    assert not store.renew(a)
    assert not store.succeed(a, {"answer": "too late"})
    b = store.claim("same-worker")
    assert b is not None and b.attempt == 2
    assert b.id == original.id and b.payload == a.payload and b.content_hash == a.content_hash
    assert not store.renew(a)
    assert not store.fail(a, "research_failed")
    assert not store.succeed(a, {"answer": "stale"})
    assert store.succeed(b, {"answer": "accepted"})
    assert not store.succeed(b, {"answer": "overwrite"})
    final = store.get(original.id)
    assert final is not None and final.result == {"answer": "accepted"}
    assert final.lease_until is None


def test_expired_last_attempt_is_terminal(tmp_path: Path) -> None:
    now = [100.0]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    task, _ = store.submit("req", "question", "a" * 64)
    for attempt in range(1, 4):
        claimed = store.claim("worker", lease_seconds=1)
        assert claimed is not None and claimed.attempt == attempt
        now[0] += 1
    assert store.claim("next-worker") is None
    final = store.get(task.id)
    assert final is not None and final.status == "failed" and final.error_code == "attempts_exhausted"


@pytest.mark.parametrize("key,question,digest", [
    ("bad key", "q", "a" * 64), ("req", "  ", "a" * 64),
    ("req", "q" * 4001, "a" * 64), ("req", "q", "invalid"),
])
def test_invalid_submission_rejected(tmp_path: Path, key: str, question: str, digest: str) -> None:
    with pytest.raises(ValueError):
        ResearchTaskStore(tmp_path / "tasks.sqlite3").submit(key, question, digest)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_lease_rejected(tmp_path: Path, duration: float) -> None:
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3")
    with pytest.raises(ValueError):
        store.claim("worker", lease_seconds=duration)


def test_busy_database_does_not_claim_or_expose_sql(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    store = ResearchTaskStore(path, busy_timeout=0.01)
    task, _ = store.submit("req", "private question", "a" * 64)
    db = sqlite3.connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(TaskStoreError, match="^task_store_unavailable$"):
            store.claim("worker")
        # WAL readers can still see the committed task while a writer holds the lock.
        restored = store.get(task.id)
        assert restored is not None and restored.status == "pending"
    finally:
        db.close()
    assert store.claim("worker") is not None


def test_startup_waits_for_reader_during_wal_transition(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    ResearchTaskStore(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as reader:
        reader.execute("PRAGMA journal_mode=DELETE")
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM research_tasks").fetchall()
        with ThreadPoolExecutor() as executor:
            opened = executor.submit(ResearchTaskStore, path, busy_timeout=1)
            time.sleep(0.1)
            reader.rollback()
            store = opened.result(timeout=5)
    assert store.submit("req", "q", "a" * 64)[1]


def test_unknown_database_is_not_migrated(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE other (value TEXT)")
    db.commit()
    db.close()
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(TaskStoreError, match="incompatible_task_database"):
        ResearchTaskStore(path)
    assert path.read_bytes() == before


def test_unsafe_paths_rejected(tmp_path: Path) -> None:
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(TaskStoreError, match="unsafe_task_database_directory"):
        ResearchTaskStore(public / "tasks.sqlite3")
    target = tmp_path / "target"
    target.write_bytes(b"")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(TaskStoreError, match="unsafe_task_database_file"):
        ResearchTaskStore(link)
    target.chmod(0o644)
    with pytest.raises(TaskStoreError, match="unsafe_task_database_file"):
        ResearchTaskStore(target)


def test_worker_runs_real_agent_and_result_survives_restart(tmp_path: Path, corpus: Corpus) -> None:
    path = tmp_path / "tasks.sqlite3"
    store = ResearchTaskStore(path)
    task, _ = store.submit("req", "What is the Helix fixture status?", corpus.content_hash)
    worker = ResearchWorker(store, corpus, worker_id="worker")
    assert worker.run_once().status == "succeeded"
    final = ResearchTaskStore(path).get(task.id)
    assert final is not None and final.result is not None
    assert final.result["answer"]["status"] == "answered"
    assert "ready" in final.result["answer"]["answer"]
    assert worker.run_once().status == "idle"


def test_worker_rejects_corpus_and_executor_drift(tmp_path: Path, corpus: Corpus) -> None:
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.submit("req", "question", "a" * 64)
    calls = []
    worker = ResearchWorker(store, corpus, worker_id="worker", processor=lambda q: calls.append(q))
    assert worker.run_once().status == "failed"
    assert store.get(task.id).error_code == "corpus_mismatch"
    assert calls == []
    task, _ = store.submit("req2", "question", corpus.content_hash)
    payload = dict(task.payload, executor_version="future-version")
    with closing(sqlite3.connect(store.path)) as db:
        with db:
            db.execute("UPDATE research_tasks SET payload_json=? WHERE id=?", (json.dumps(payload), task.id))
    assert worker.run_once().status == "failed"
    assert store.get(task.id).error_code == "executor_mismatch"
    assert calls == []


def test_worker_failure_is_redacted(tmp_path: Path, corpus: Corpus) -> None:
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.submit("req", "private question", corpus.content_hash)

    result = ResearchWorker(store, corpus, worker_id="worker", processor=_failing_processor).run_once()
    assert result.status == "failed"
    public = store.get(task.id).public_dict()
    assert public["error_code"] == "research_failed"
    assert "private" not in json.dumps(public)
    with pytest.raises(ValueError):
        store.fail(store.get(task.id), "private exception details")


def _failing_processor(_question):
    raise RuntimeError("private exception details")


def _slow_processor(_question):
    time.sleep(1.5)
    return {"answer": "done"}


def _blocking_processor(marker: Path, _question, *, ignore_term: bool = False):
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (marker / "started").write_text(str(os.getpid()))
    while not (marker / "release").exists():
        time.sleep(0.02)
    return {"answer": "done"}


def test_heartbeat_keeps_task_owned_during_slow_processing(tmp_path: Path, corpus: Corpus) -> None:
    renewed = threading.Event()

    class ObservedStore(ResearchTaskStore):
        def renew(self, task, *, lease_seconds=30.0):
            accepted = super().renew(task, lease_seconds=lease_seconds)
            renewed.set()
            return accepted

    store = ObservedStore(tmp_path / "tasks.sqlite3")
    store.submit("req", "question", corpus.content_hash)

    worker = ResearchWorker(store, corpus, worker_id="worker", lease_seconds=3, processor=_slow_processor)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(worker.run_once)
        assert renewed.wait(timeout=5)
        assert store.claim("competitor") is None
        assert future.result(timeout=5).status == "succeeded"


@pytest.mark.parametrize("renewal", ["refused", "error"])
def test_heartbeat_loss_does_not_write_result(tmp_path: Path, corpus: Corpus, renewal: str) -> None:
    attempted = threading.Event()

    class LostStore(ResearchTaskStore):
        def renew(self, task, *, lease_seconds=30.0):
            attempted.set()
            if renewal == "error":
                raise TaskStoreError("task_store_unavailable")
            return False

    store = LostStore(tmp_path / "tasks.sqlite3")
    task, _ = store.submit("req", "question", corpus.content_hash)

    outcome = ResearchWorker(store, corpus, worker_id="worker", lease_seconds=0.3, processor=_slow_processor).run_once()
    assert outcome.status == "lost_lease"
    assert attempted.is_set()
    assert store.get(task.id).result is None


_CRASH_WORKER = '''
import sys
from pathlib import Path
from agentlab.research.agent import ResearchAgent
from agentlab.research.corpus import Corpus
from agentlab.research.tasks import ResearchTaskStore
from agentlab.research.worker import ResearchWorker

path, root, phase = sys.argv[1:]
corpus = Corpus.load(Path(root))
def pause():
    print("ready", flush=True)
    sys.stdin.readline()
class CrashStore(ResearchTaskStore):
    def claim(self, *args, **kwargs):
        task = super().claim(*args, **kwargs)
        if phase == "before":
            pause()
        return task
    def succeed(self, task, result):
        if phase == "after":
            assert result["answer"]["status"] == "answered"
            pause()
        return super().succeed(task, result)
ResearchWorker(CrashStore(Path(path)), corpus, worker_id="worker-a").run_once()
'''


@pytest.mark.parametrize("phase", ["before", "after"])
def test_terminated_process_recovers_at_both_crash_windows(tmp_path: Path, corpus: Corpus, phase: str) -> None:
    path = tmp_path / "tasks.sqlite3"
    store = ResearchTaskStore(path)
    task, _ = store.submit("req", "What is the Helix fixture status?", corpus.content_hash)
    process = subprocess.Popen(
        [sys.executable, "-c", _CRASH_WORKER, str(path), str(tmp_path / "corpus"), phase],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "worker did not reach the crash window"
            assert process.stdout.readline().strip() == "ready"
        running = store.get(task.id)
        assert running.status == "running" and running.attempt == 1 and running.result is None
        process.terminate()
        process.communicate(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    # Advance the shared clock only after the actual process was terminated.
    recovered = ResearchTaskStore(path, clock=lambda: running.lease_until + 1)
    outcome = ResearchWorker(recovered, corpus, worker_id="worker-b").run_once()
    assert outcome.status == "succeeded" and outcome.attempt == 2
    final = recovered.get(task.id)
    assert final.payload == task.payload and final.content_hash == task.content_hash
    assert final.result["answer"]["status"] == "answered"


def test_deadline_includes_queue_and_is_not_reset_on_takeover(tmp_path: Path) -> None:
    now = [100.0]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    queued, _ = store.submit("queued", "q", "a" * 64)
    now[0] += TASK_TIMEOUT_SECONDS
    assert store.claim("worker") is None
    assert store.get(queued.id).error_code == "deadline_exceeded"
    task, _ = store.submit("running", "q", "a" * 64)
    a = store.claim("worker-a", lease_seconds=10)
    now[0] += 10
    b = store.claim("worker-b", lease_seconds=1000)
    assert b.attempt == 2 and b.deadline_at == task.deadline_at
    assert b.lease_until == task.deadline_at
    now[0] = task.deadline_at
    assert not store.renew(b)
    assert not store.succeed(a, {"answer": "old"})
    assert not store.succeed(b, {"answer": "late"})
    assert store.get(task.id).error_code == "deadline_exceeded"


def test_get_closes_expired_task_and_terminal_result_is_stable(tmp_path: Path) -> None:
    now = [100.0]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    task, _ = store.submit("req", "q", "a" * 64)
    now[0] = task.deadline_at
    terminal = store.get(task.id)
    assert terminal.error_code == "deadline_exceeded"
    assert store.cancel(task.id) == terminal
    assert store.submit("req", "q", "a" * 64) == (terminal, False)


@pytest.mark.parametrize("running", [False, True])
def test_cancel_pending_or_running_is_idempotent(tmp_path: Path, running: bool) -> None:
    path = tmp_path / "tasks.sqlite3"
    store = ResearchTaskStore(path)
    task, _ = store.submit("req", "q", "a" * 64)
    claim = store.claim("worker") if running else None
    cancelled = store.cancel(task.id)
    assert cancelled.status == "failed" and cancelled.error_code == "cancelled"
    assert cancelled.lease_until is None
    assert ResearchTaskStore(path).cancel(task.id) == cancelled
    assert store.submit("req", "q", "a" * 64) == (cancelled, False)
    assert store.cancel("missing") is None
    assert store.claim("worker-b") is None
    if claim is not None:
        assert not store.renew(claim)
        assert not store.succeed(claim, {"answer": "late"})


def test_cancel_and_finish_race_has_one_stable_winner(tmp_path: Path) -> None:
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.submit("req", "q", "a" * 64)
    claim = store.claim("worker")
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait(timeout=5)
        return store.succeed(claim, {"answer": "accepted"})

    def cancel():
        barrier.wait(timeout=5)
        return store.cancel(task.id)

    with ThreadPoolExecutor() as executor:
        completed = executor.submit(finish)
        cancelled = executor.submit(cancel)
        accepted = completed.result(timeout=5)
        terminal = cancelled.result(timeout=5)
    assert terminal == store.get(task.id) == store.cancel(task.id)
    assert (terminal.status == "succeeded") == accepted
    assert terminal.result == ({"answer": "accepted"} if accepted else None)


def test_capacity_replay_conflict_and_release(tmp_path: Path) -> None:
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3")
    for i in range(MAX_OUTSTANDING_TASKS):
        store.submit(f"req-{i}", "q", "a" * 64)
    existing, created = store.submit("req-0", "q", "a" * 64)
    assert not created
    with pytest.raises(TaskConflict):
        store.submit("req-0", "changed", "a" * 64)
    with pytest.raises(TaskCapacityExceeded):
        store.submit("overflow", "q", "a" * 64)
    store.cancel(existing.id)
    assert store.submit("overflow", "q", "a" * 64)[1]


def test_expiry_frees_capacity(tmp_path: Path) -> None:
    now = [100.0]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    for i in range(MAX_OUTSTANDING_TASKS):
        store.submit(f"req-{i}", "q", "a" * 64)
    now[0] += TASK_TIMEOUT_SECONDS
    assert store.submit("next", "q", "a" * 64)[1]


def test_metrics_snapshot_exposes_expired_leases_and_deadlines(tmp_path: Path) -> None:
    now = [100.0]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    store.submit("req", "Private question", "a" * 64)
    assert store.metrics()["oldest_pending_age_ms"] == 0
    now[0] += 5
    assert store.metrics()["oldest_pending_age_ms"] == 5000
    store.claim("private-worker", lease_seconds=1)
    now[0] += 2
    metrics = store.metrics()
    assert metrics["expired_leases"] == 1 and metrics["overdue_tasks"] == 0
    now[0] = 400
    metrics = store.metrics()
    assert metrics["outstanding"] == metrics["overdue_tasks"] == 1
    assert metrics["available_capacity"] == 100
    assert metrics["counts"]["running"] == 1  # Read-only snapshot doesn't mutate tasks.
    store.claim("next-worker")
    metrics = store.metrics()
    assert metrics["errors"] == {"deadline_exceeded": 1}
    assert metrics["outstanding"] == 0
    with closing(sqlite3.connect(store.path)) as db:
        with db:
            db.execute("UPDATE research_tasks SET error_code='private-exception-marker'")
    assert store.metrics()["errors"] == {"other": 1}


def test_event_allowlist_and_broken_sink(capsys, monkeypatch) -> None:
    emit_task_event("worker_finished", task_id="private-id", status="failed", error_code="private-error",
                    question="private-question", request_key="private-key", duration_ms=10,
                    queue_wait_ms=float("inf"), attempt=True)
    assert json.loads(capsys.readouterr().err) == {"event": "worker_finished", "status": "failed", "duration_ms": 10}
    emit_task_event("private-event")
    assert capsys.readouterr().err == ""

    class BrokenSink:
        def write(self, text):
            raise OSError("private sink error")

    monkeypatch.setattr(sys, "stderr", BrokenSink())
    emit_task_event("worker_error", error_code="task_store_unavailable")


def test_capacity_limit_is_atomic_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    store = ResearchTaskStore(path, clock=lambda: 100.0)
    for i in range(MAX_OUTSTANDING_TASKS - 1):
        store.submit(f"req-{i}", "q", "a" * 64)
    results = _race(path, "capacity")
    assert sum(created for _, created in results) == 1
    assert sum(name == "full" for name, _ in results) == 3


def _wait_for_marker(path: Path) -> int:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return int(path.read_text())
        time.sleep(0.01)
    raise AssertionError("child did not start")


@pytest.mark.parametrize("reason", ["cancelled", "deadline_exceeded"])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_blocked_child_is_reaped_and_worker_can_continue(
    tmp_path: Path, corpus: Corpus, reason: str, ignore_term: bool,
) -> None:
    now = [time.time()]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    task, _ = store.submit("req", "q", corpus.content_hash)
    worker = ResearchWorker(store, corpus, worker_id="worker", lease_seconds=1,
                            processor=partial(_blocking_processor, tmp_path, ignore_term=ignore_term))
    with ThreadPoolExecutor() as executor:
        future = executor.submit(worker.run_once)
        pid = _wait_for_marker(tmp_path / "started")
        if reason == "cancelled":
            store.cancel(task.id)
        else:
            now[0] = task.deadline_at
        assert future.result(timeout=8).status == reason
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert store.get(task.id).result is None
    (tmp_path / "release").write_text("go")
    store.submit("next", "q", corpus.content_hash)
    assert worker.run_once().status == "succeeded"


def test_cancel_after_execution_before_commit_wins(tmp_path: Path, corpus: Corpus) -> None:
    class CancelAtCommit(ResearchTaskStore):
        def succeed(self, task, result):
            self.cancel(task.id)
            return super().succeed(task, result)

    store = CancelAtCommit(tmp_path / "tasks.sqlite3")
    task, _ = store.submit("req", "What is the Helix fixture status?", corpus.content_hash)
    outcome = ResearchWorker(store, corpus, worker_id="worker").run_once()
    assert outcome.status == "cancelled"
    assert store.get(task.id).result is None


def test_real_budget_stops_blocked_child_even_with_heartbeats(tmp_path: Path, corpus: Corpus) -> None:
    # Simulate 298 seconds already spent queueing, then use the real wall clock.
    path = tmp_path / "tasks.sqlite3"
    old_store = ResearchTaskStore(path, clock=lambda: time.time() - TASK_TIMEOUT_SECONDS + 2)
    task, _ = old_store.submit("req", "q", corpus.content_hash)
    store = ResearchTaskStore(path)
    worker = ResearchWorker(store, corpus, worker_id="worker", lease_seconds=0.6,
                            processor=partial(_blocking_processor, tmp_path))
    started = time.monotonic()
    outcome = worker.run_once()
    assert outcome.status == "deadline_exceeded"
    assert time.monotonic() - started < 8
    pid = int((tmp_path / "started").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert store.get(task.id).error_code == "deadline_exceeded"


def test_monotonic_budget_survives_wall_clock_rollback(tmp_path: Path, corpus: Corpus) -> None:
    now = [time.time()]
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    task, _ = store.submit("req", "q", corpus.content_hash)
    now[0] = task.deadline_at - 2
    worker = ResearchWorker(store, corpus, worker_id="worker", lease_seconds=30,
                            processor=partial(_blocking_processor, tmp_path))
    with ThreadPoolExecutor() as executor:
        future = executor.submit(worker.run_once)
        pid = _wait_for_marker(tmp_path / "started")
        now[0] -= 100
        assert future.result(timeout=8).status == "deadline_exceeded"
    assert store.get(task.id).error_code == "deadline_exceeded"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cli_sigterm_reaps_active_child_and_preserves_unfinished_task(tmp_path: Path, corpus: Corpus) -> None:
    store = ResearchTaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.submit("req", "q", corpus.content_hash)
    script = '''
import sys
from pathlib import Path
from functools import partial
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_research_tasks import _blocking_processor
import agentlab.cli as cli
root = Path(sys.argv[1])
cli.ResearchWorker = partial(cli.ResearchWorker, processor=partial(_blocking_processor, root))
raise SystemExit(cli.main(["research-worker", "--corpus", str(root / "corpus"),
    "--task-db", str(root / "tasks.sqlite3")]))
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        pid = _wait_for_marker(tmp_path / "started")
        process.terminate()
        _, errors = process.communicate(timeout=5)
        assert process.returncode == 130
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert store.get(task.id).status == "running"
        events = [json.loads(line) for line in errors.splitlines() if line.startswith("{")]
        assert events[-1]["event"] == "worker_error" and events[-1]["status"] == "interrupted"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_child_stops_when_worker_is_killed(tmp_path: Path, corpus: Corpus) -> None:
    path = tmp_path / "tasks.sqlite3"
    store = ResearchTaskStore(path)
    task, _ = store.submit("req", "q", corpus.content_hash)
    script = '''
import sys
from pathlib import Path
from functools import partial
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_research_tasks import _blocking_processor
from agentlab.research.corpus import Corpus
from agentlab.research.tasks import ResearchTaskStore
from agentlab.research.worker import ResearchWorker
root = Path(sys.argv[1])
ResearchWorker(ResearchTaskStore(root / "tasks.sqlite3"), Corpus.load(root / "corpus"),
    worker_id="doomed-worker", processor=partial(_blocking_processor, root)).run_once()
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pid = _wait_for_marker(tmp_path / "started")
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # Orphans can briefly remain zombies awaiting init's reap.
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("orphan execution process remained alive")
        assert store.get(task.id).status == "running"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
