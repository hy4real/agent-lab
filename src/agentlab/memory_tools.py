"""Strict, bounded on-demand search over confirmed long-term memory."""

from __future__ import annotations

import json

from .longterm import LongTermMemory, LongTermMemoryError, MemoryRecord
from .messages import estimate_tokens
from .reasoning import ToolError, ToolObservation, ToolRunner


MEMORY_SEARCH_TOOL = "memory_search"
MEMORY_SEARCH_MAX_CALLS = 2
MEMORY_SEARCH_MAX_RESULTS = 5
MEMORY_SEARCH_BUDGET_TOKENS = 1024
_MAX_QUERY_LENGTH = 500


class MemorySearchToolRunner:
    """Per-run ToolRunner wrapper; call limits reset for every instance."""

    def __init__(
        self,
        delegate: ToolRunner,
        memory: LongTermMemory,
        *,
        scope: str,
        max_calls: int = MEMORY_SEARCH_MAX_CALLS,
        budget_tokens: int = MEMORY_SEARCH_BUDGET_TOKENS,
    ) -> None:
        if MEMORY_SEARCH_TOOL in delegate.names():
            raise ValueError("memory_search 工具名称冲突")
        if max_calls <= 0 or budget_tokens <= 0:
            raise ValueError("memory_search 限额必须为正整数")
        self._delegate = delegate
        self._memory = memory
        self._scope = scope
        self._max_calls = max_calls
        self._budget_tokens = budget_tokens
        self._calls = 0

    def names(self) -> tuple[str, ...]:
        return tuple(sorted((*self._delegate.names(), MEMORY_SEARCH_TOOL)))

    def call(self, tool: str, argument: str) -> str | ToolObservation:
        if tool != MEMORY_SEARCH_TOOL:
            return self._delegate.call(tool, argument)
        self._calls += 1
        if self._calls > self._max_calls:
            raise ToolError("memory_search_limit")
        query, top_k = _parse_arguments(argument)
        try:
            ranked = self._memory.search(query, scope=self._scope, top_k=top_k)
        except LongTermMemoryError as exc:
            raise ToolError("memory_search_failed") from exc
        selected = _pack_records(ranked, budget_tokens=self._budget_tokens)
        content = _render_result(selected)
        return ToolObservation(
            content=content,
            trace_detail=f"memory_search:ok:{len(selected)}",
        )

    @property
    def calls(self) -> int:
        return self._calls


def _parse_arguments(argument: str) -> tuple[str, int]:
    try:
        value = json.loads(argument)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ToolError("invalid_arguments") from exc
    if not isinstance(value, dict) or set(value) - {"query", "top_k"} or "query" not in value:
        raise ToolError("invalid_arguments")
    query = value["query"]
    top_k = value.get("top_k", MEMORY_SEARCH_MAX_RESULTS)
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > _MAX_QUERY_LENGTH
        or isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or top_k < 1
        or top_k > MEMORY_SEARCH_MAX_RESULTS
    ):
        raise ToolError("invalid_arguments")
    return query, top_k


def _record_view(record: MemoryRecord) -> dict[str, str]:
    return {
        "id": record.id,
        "kind": record.kind,
        "origin": record.origin,
        "text": record.text,
    }


def _render_result(records: tuple[MemoryRecord, ...]) -> str:
    return json.dumps(
        {
            "ok": True,
            "tool": MEMORY_SEARCH_TOOL,
            "data": [_record_view(record) for record in records],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pack_records(
    records: tuple[MemoryRecord, ...],
    *,
    budget_tokens: int,
) -> tuple[MemoryRecord, ...]:
    selected: list[MemoryRecord] = []
    for record in records:
        candidate = tuple((*selected, record))
        if estimate_tokens(_render_result(candidate)) <= budget_tokens:
            selected.append(record)
    return tuple(selected)
