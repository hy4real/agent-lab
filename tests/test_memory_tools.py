"""On-demand memory search tool contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentlab.environment import AgentEnvironment
from agentlab.longterm import LongTermMemory
from agentlab.memory import ShortTermMemory
from agentlab.memory_tools import MemorySearchToolRunner
from agentlab.reasoning import (
    DeterministicReasoner,
    ToolError,
    ToolExecutor,
    ToolObservation,
)


def _clock() -> str:
    return "2026-08-14T00:00:00+00:00"


def _confirmed(memory: LongTermMemory, text: str, *, scope: str, revision: int) -> int:
    record = memory.propose(
        text,
        kind="note",
        scope=scope,
        origin="manual",
        expected_revision=revision,
    )
    memory.confirm(record.id, scope=scope, expected_revision=revision + 1)
    return revision + 2


def test_memory_search_is_strict_bounded_and_scope_isolated(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    revision = _confirmed(memory, "Alpha 使用本地缓存", scope="project:alpha", revision=0)
    _confirmed(memory, "Beta 使用远端缓存", scope="project:beta", revision=revision)
    tools = MemorySearchToolRunner(
        ToolExecutor({"other": "ok"}),
        memory,
        scope="project:alpha",
    )
    result = tools.call("memory_search", json.dumps({"query": "缓存", "top_k": 5}))
    assert isinstance(result, ToolObservation)
    payload = json.loads(result.content)
    assert [item["text"] for item in payload["data"]] == ["Alpha 使用本地缓存"]
    assert result.trace_detail == "memory_search:ok:1"
    assert tools.names() == ("memory_search", "other")


@pytest.mark.parametrize(
    "argument",
    [
        "broken",
        "[]",
        "{}",
        '{"query":""}',
        '{"query":"x","top_k":0}',
        '{"query":"x","top_k":6}',
        '{"query":"x","extra":true}',
    ],
)
def test_memory_search_rejects_invalid_arguments(tmp_path: Path, argument: str) -> None:
    tools = MemorySearchToolRunner(
        ToolExecutor({"other": "ok"}),
        LongTermMemory(state_path=tmp_path / "longterm.json"),
        scope="local",
    )
    with pytest.raises(ToolError, match="invalid_arguments"):
        tools.call("memory_search", argument)


def test_memory_search_enforces_per_run_call_limit(tmp_path: Path) -> None:
    tools = MemorySearchToolRunner(
        ToolExecutor({"other": "ok"}),
        LongTermMemory(state_path=tmp_path / "longterm.json"),
        scope="local",
    )
    argument = '{"query":"cache"}'
    tools.call("memory_search", argument)
    tools.call("memory_search", argument)
    with pytest.raises(ToolError, match="memory_search_limit"):
        tools.call("memory_search", argument)


def test_memory_search_skips_whole_oversized_records(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    memory.add("cache " + "x" * 1900)
    memory.add("cache uses LRU")
    tools = MemorySearchToolRunner(
        ToolExecutor({"other": "ok"}),
        memory,
        scope="local",
        budget_tokens=80,
    )
    result = tools.call("memory_search", '{"query":"cache"}')
    assert isinstance(result, ToolObservation)
    payload = json.loads(result.content)
    assert [item["text"] for item in payload["data"]] == ["cache uses LRU"]


def test_memory_search_delegates_other_tools(tmp_path: Path) -> None:
    tools = MemorySearchToolRunner(
        ToolExecutor({"other": "delegate-result"}),
        LongTermMemory(state_path=tmp_path / "longterm.json"),
        scope="local",
    )
    assert tools.call("other", "argument") == "delegate-result"


def test_environment_memory_search_keeps_private_content_out_of_trace(tmp_path: Path) -> None:
    secret = "private-memory-text"
    query = "private-query"
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    _confirmed(memory, f"{query} {secret}", scope="project:alpha", revision=0)
    reasoner = DeterministicReasoner(
        tool_sequence={"task": (("memory_search", json.dumps({"query": query})),)},
        answers={"task": "done"},
    )
    environment = AgentEnvironment(
        tools=ToolExecutor({"other": "ok"}),
        reasoner=reasoner,
    )
    short_term = ShortTermMemory(budget_tokens=2000)
    run = environment.run(
        "task",
        short_term=short_term,
        long_term=memory,
        memory_scope="project:alpha",
        enable_memory_search=True,
    )
    trace_text = "\n".join(record.detail for record in run.trace.records)
    assert secret not in trace_text
    assert query not in trace_text
    assert "memory_search:ok:1" in trace_text
    session_text = "\n".join(message.content for message in short_term.state().recent_messages)
    assert secret in session_text
