"""Allowlisted local task diagnostics; no user input or exception bodies."""

from __future__ import annotations

import json
import math
import re
import sys
from typing import Any

ERROR_CODES = frozenset({
    "cancelled", "deadline_exceeded", "attempts_exhausted", "corpus_mismatch",
    "executor_mismatch", "research_failed", "task_store_unavailable",
    "idempotency_conflict", "task_capacity_exceeded",
})
_EVENTS = frozenset({"task_submitted", "task_replayed", "task_cancelled", "task_rejected",
                     "worker_claimed", "worker_finished", "worker_error"})
_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "cancelled",
                       "deadline_exceeded", "lost_lease", "interrupted"})


def emit_task_event(event: str, **fields: Any) -> None:
    if event not in _EVENTS:
        return
    record: dict[str, Any] = {"event": event}
    task_id = fields.get("task_id")
    if isinstance(task_id, str) and re.fullmatch(r"[0-9a-f]{32}", task_id):
        record["task_id"] = task_id
    if fields.get("status") in _STATUSES:
        record["status"] = fields["status"]
    if fields.get("error_code") in ERROR_CODES:
        record["error_code"] = fields["error_code"]
    for name in ("attempt", "queue_wait_ms", "duration_ms"):
        value = fields.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            record[name] = value
    try:
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # A diagnostics sink must not change whether a committed operation succeeded.
        pass
