"""Local SQLite task ledger. Transactions never span Agent execution."""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .task_events import ERROR_CODES

EXECUTOR_VERSION = "research-offline-v1"
TASK_TIMEOUT_SECONDS = 300.0
MAX_OUTSTANDING_TASKS = 100
_APPLICATION_ID = 0x41474C54
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class TaskStoreError(RuntimeError):
    """Stable error without database paths, SQL or input contents."""


class TaskConflict(TaskStoreError):
    pass


class TaskCapacityExceeded(TaskStoreError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid_identifier")


def validate_lease(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_lease")


@dataclass(frozen=True)
class ResearchTask:
    id: str
    payload: dict[str, Any]
    content_hash: str
    corpus_hash: str
    status: str
    worker_id: str | None
    attempt: int
    max_attempts: int
    lease_until: float | None
    result: dict[str, Any] | None
    error_code: str | None
    created_at: float
    updated_at: float

    @property
    def deadline_at(self) -> float:
        return self.created_at + TASK_TIMEOUT_SECONDS

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ResearchTask:
        return cls(
            id=row["id"], payload=json.loads(row["payload_json"]),
            content_hash=row["content_hash"], corpus_hash=row["corpus_hash"],
            status=row["status"], worker_id=row["worker_id"], attempt=row["attempt"],
            max_attempts=row["max_attempts"], lease_until=row["lease_until"],
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
            error_code=row["error_code"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "status": self.status, "attempt": self.attempt,
            "result": self.result, "error_code": self.error_code,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "deadline_at": self.deadline_at,
        }


class ResearchTaskStore:
    """One connection per operation; supports processes sharing a local database."""

    def __init__(
        self, path: Path, *, clock: Callable[[], float] = time.time,
        busy_timeout: float = 5.0,
    ) -> None:
        self.path = Path(path).absolute()
        self.clock = clock
        if not math.isfinite(busy_timeout) or busy_timeout < 0:
            raise ValueError("invalid_busy_timeout")
        self.busy_timeout = busy_timeout
        # Schema commit and journal-mode transition are separate SQLite operations.
        # Serialize the whole startup sequence, including sidecar inspection.
        with self._initialization_lock():
            self._prepare_path()
            self._initialize()

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        descriptor = None
        try:
            parent = self.path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.getuid()):
                raise TaskStoreError("unsafe_task_database_directory")
            lock_path = self.path.with_name(self.path.name + ".init.lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_nlink != 1):
                raise TaskStoreError("unsafe_task_database_file")
            deadline = time.monotonic() + self.busy_timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TaskStoreError("task_store_unavailable") from None
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            yield
        except OSError:
            raise TaskStoreError("task_store_unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)  # Releases flock even when initialization fails.

    def _initialize(self) -> None:
        with self._connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and application == 0 and not tables:
                db.execute("""
                    CREATE TABLE research_tasks (
                        id TEXT PRIMARY KEY,
                        request_key TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        corpus_hash TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
                        worker_id TEXT,
                        attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
                        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
                        lease_until REAL,
                        result_json TEXT,
                        error_code TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        CHECK(attempt <= max_attempts),
                        CHECK((status = 'running') = (lease_until IS NOT NULL)),
                        CHECK(status != 'running' OR (worker_id IS NOT NULL AND attempt > 0)),
                        CHECK(status != 'pending' OR (worker_id IS NULL AND attempt = 0)),
                        CHECK((status = 'succeeded') = (result_json IS NOT NULL)),
                        CHECK((status = 'failed') = (error_code IS NOT NULL))
                    )
                """)
                db.execute("CREATE INDEX research_tasks_claim ON research_tasks(status, lease_until, created_at)")
                db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                db.execute("PRAGMA user_version = 1")
            elif version != 1 or application != _APPLICATION_ID:
                raise TaskStoreError("incompatible_task_database")
        with self._connection() as db:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            if mode != "wal":
                mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise TaskStoreError("task_store_unavailable")

    def _prepare_path(self) -> None:
        try:
            parent = self.path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.getuid()):
                raise TaskStoreError("unsafe_task_database_directory")
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
                    raise TaskStoreError("unsafe_task_database_file")
        except OSError:
            raise TaskStoreError("task_store_unavailable") from None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            db = sqlite3.connect(
                self.path.as_uri() + "?mode=rw", uri=True,
                timeout=self.busy_timeout, isolation_level=None,
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous = FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except sqlite3.Error:
            raise TaskStoreError("task_store_unavailable") from None
        finally:
            if db is not None:
                db.close()  # Also rolls back an uncommitted transaction on every error path.

    def submit(self, request_key: str, question: str, corpus_hash: str) -> tuple[ResearchTask, bool]:
        validate_identifier(request_key)
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError("invalid_question")
        if not isinstance(corpus_hash, str) or not _HASH.fullmatch(corpus_hash):
            raise ValueError("invalid_corpus_hash")
        payload = _json({"question": question, "corpus_hash": corpus_hash, "executor_version": EXECUTOR_VERSION})
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._connection(write=True) as db:
            now = self.clock()
            self._expire(db, now)
            existing = db.execute("SELECT * FROM research_tasks WHERE request_key = ?", (request_key,)).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload or existing["content_hash"] != digest:
                    raise TaskConflict("idempotency_conflict")
                return ResearchTask.from_row(existing), False
            outstanding = db.execute(
                "SELECT count(*) FROM research_tasks WHERE status IN ('pending','running')"
            ).fetchone()[0]
            if outstanding >= MAX_OUTSTANDING_TASKS:
                raise TaskCapacityExceeded("task_capacity_exceeded")
            task_id = uuid.uuid4().hex
            db.execute("""
                INSERT INTO research_tasks
                    (id, request_key, payload_json, content_hash, corpus_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (task_id, request_key, payload, digest, corpus_hash, now, now))
            row = db.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
            return ResearchTask.from_row(row), True

    def get(self, task_id: str) -> ResearchTask | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        task = ResearchTask.from_row(row)
        if task.status in {"pending", "running"} and task.deadline_at <= self.clock():
            with self._connection(write=True) as db:
                self._expire(db, self.clock())
                task = ResearchTask.from_row(db.execute(
                    "SELECT * FROM research_tasks WHERE id=?", (task_id,)
                ).fetchone())
        return task

    def metrics(self) -> dict[str, Any]:
        with self._connection() as db:
            db.execute("BEGIN")
            now = self.clock()
            cutoff = now - TASK_TIMEOUT_SECONDS
            counts = {status: 0 for status in ("pending", "running", "succeeded", "failed")}
            counts.update({row[0]: row[1] for row in db.execute(
                "SELECT status, count(*) FROM research_tasks GROUP BY status"
            )})
            errors: dict[str, int] = {}
            for code, count in db.execute(
                "SELECT error_code, count(*) FROM research_tasks WHERE status='failed' GROUP BY error_code"
            ):
                key = code if code in ERROR_CODES else "other"
                errors[key] = errors.get(key, 0) + count
            overdue = db.execute("""
                SELECT count(*) FROM research_tasks
                WHERE status IN ('pending','running') AND created_at <= ?
            """, (cutoff,)).fetchone()[0]
            expired = db.execute("""
                SELECT count(*) FROM research_tasks WHERE status='running' AND lease_until <= ?
            """, (now,)).fetchone()[0]
            oldest = db.execute("SELECT min(created_at) FROM research_tasks WHERE status='pending'").fetchone()[0]
            outstanding = counts["pending"] + counts["running"]
            return {
                "storage_status": "ok", "counts": counts, "errors": errors,
                "outstanding": outstanding, "capacity": MAX_OUTSTANDING_TASKS,
                "available_capacity": max(0, MAX_OUTSTANDING_TASKS - outstanding + overdue),
                "overdue_tasks": overdue, "expired_leases": expired,
                "oldest_pending_age_ms": max(0, round((now - oldest) * 1000)) if oldest is not None else 0,
            }

    @staticmethod
    def _expire(db: sqlite3.Connection, now: float) -> None:
        db.execute("""
            UPDATE research_tasks SET status='failed', error_code='deadline_exceeded',
                lease_until=NULL, updated_at=?
            WHERE status IN ('pending','running') AND created_at <= ?
        """, (now, now - TASK_TIMEOUT_SECONDS))

    def cancel(self, task_id: str) -> ResearchTask | None:
        with self._connection(write=True) as db:
            now = self.clock()
            self._expire(db, now)
            db.execute("""
                UPDATE research_tasks SET status='failed', error_code='cancelled',
                    lease_until=NULL, updated_at=?
                WHERE id=? AND status IN ('pending','running')
            """, (now, task_id))
            row = db.execute("SELECT * FROM research_tasks WHERE id=?", (task_id,)).fetchone()
            return ResearchTask.from_row(row) if row is not None else None

    def claim(self, worker_id: str, *, lease_seconds: float = 30.0) -> ResearchTask | None:
        validate_identifier(worker_id)
        validate_lease(lease_seconds)
        with self._connection(write=True) as db:
            now = self.clock()
            self._expire(db, now)
            db.execute("""
                UPDATE research_tasks SET status='failed', error_code='attempts_exhausted',
                    lease_until=NULL, updated_at=?
                WHERE status='running' AND lease_until <= ? AND attempt >= max_attempts
            """, (now, now))
            row = db.execute("""
                SELECT id, created_at FROM research_tasks
                WHERE (status='pending' OR (status='running' AND lease_until <= ?))
                    AND attempt < max_attempts
                ORDER BY created_at, id LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            db.execute("""
                UPDATE research_tasks SET status='running', worker_id=?, attempt=attempt+1,
                    lease_until=?, updated_at=? WHERE id=?
            """, (worker_id, min(now + lease_seconds, row["created_at"] + TASK_TIMEOUT_SECONDS), now, row["id"]))
            return ResearchTask.from_row(db.execute(
                "SELECT * FROM research_tasks WHERE id=?", (row["id"],)
            ).fetchone())

    def renew(self, task: ResearchTask, *, lease_seconds: float = 30.0) -> bool:
        validate_lease(lease_seconds)
        with self._connection(write=True) as db:
            now = self.clock()
            self._expire(db, now)
            changed = db.execute("""
                UPDATE research_tasks SET lease_until=?, updated_at=?
                WHERE id=? AND status='running' AND worker_id=? AND attempt=? AND lease_until > ?
            """, (min(now + lease_seconds, task.deadline_at), now, task.id, task.worker_id, task.attempt, now))
            return changed.rowcount == 1

    def succeed(self, task: ResearchTask, result: dict[str, Any]) -> bool:
        return self._finish(task, result_json=_json(result), error_code=None)

    def fail(self, task: ResearchTask, error_code: str) -> bool:
        if error_code not in {"corpus_mismatch", "executor_mismatch", "research_failed", "deadline_exceeded"}:
            raise ValueError("invalid_task_error")
        return self._finish(task, result_json=None, error_code=error_code)

    def _finish(self, task: ResearchTask, *, result_json: str | None, error_code: str | None) -> bool:
        with self._connection(write=True) as db:
            now = self.clock()
            self._expire(db, now)
            changed = db.execute("""
                UPDATE research_tasks SET status=?, result_json=?, error_code=?, lease_until=NULL, updated_at=?
                WHERE id=? AND status='running' AND worker_id=? AND attempt=? AND lease_until > ?
            """, (
                "succeeded" if error_code is None else "failed", result_json, error_code, now,
                task.id, task.worker_id, task.attempt, now,
            ))
            return changed.rowcount == 1
