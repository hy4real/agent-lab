"""Research tool registry tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentlab.reasoning import ToolError
from agentlab.research.corpus import Corpus
from agentlab.research.tools import ToolRegistry, ToolSpec, build_research_tools


def _tools(tmp_path: Path):
    (tmp_path / "a.md").write_text("缓存使用 LRU 策略。", encoding="utf-8")
    return build_research_tools(Corpus.load(tmp_path))


def test_tools_list_search_and_read(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    listed = json.loads(tools.call("list_sources", "{}"))
    assert listed["tool"] == "list_sources"
    searched = json.loads(tools.call("search_corpus", '{"query":"LRU","top_k":1}'))
    chunk_id = searched["data"][0]["chunk_id"]
    read = json.loads(tools.call("read_chunk", json.dumps({"chunk_id": chunk_id})))
    assert read["data"]["text"] == "缓存使用 LRU 策略。"
    assert [event.status for event in tools.events()] == ["ok", "ok", "ok"]


@pytest.mark.parametrize(
    ("tool", "argument"),
    [
        ("missing", "{}"),
        ("list_sources", "not-json"),
        ("list_sources", '{"extra":1}'),
        ("search_corpus", '{"query":"x","top_k":true}'),
        ("read_chunk", '{"chunk_id":"unknown"}'),
    ],
)
def test_tools_fail_with_stable_errors(tmp_path: Path, tool: str, argument: str) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(ToolError) as caught:
        tools.call(tool, argument)
    assert str(caught.value) in {"unknown_tool", "invalid_arguments", "tool_failure"}
    assert tools.events()[-1].status == "error"


def test_unexpected_handler_error_is_sanitized() -> None:
    registry = ToolRegistry()

    def fail(_args):
        raise KeyError("private handler detail")

    registry.register(
        "fail",
        ToolSpec(required={}, optional={}, handler=fail, side_effect="read", max_output_bytes=8 * 1024),
    )
    with pytest.raises(ToolError, match="^tool_failure$"):
        registry.call("fail", "{}")


def test_tool_spec_requires_explicit_side_effect_and_output_limit() -> None:
    """AGENTS.md: every ToolSpec must declare what it may touch and how big it may get."""
    with pytest.raises(ValueError, match="side_effect"):
        ToolSpec(required={}, optional={}, handler=lambda _a: 1, side_effect="network", max_output_bytes=64)
    with pytest.raises(ValueError, match="max_output_bytes"):
        ToolSpec(required={}, optional={}, handler=lambda _a: 1, side_effect="none", max_output_bytes=0)


def test_declared_output_limit_is_enforced(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        "verbose",
        ToolSpec(
            required={},
            optional={},
            handler=lambda _a: "x" * 4096,
            side_effect="none",
            max_output_bytes=256,
        ),
    )
    with pytest.raises(ToolError, match="^output_too_large$"):
        registry.call("verbose", "{}")
    assert registry.events()[-1].status == "error"


def test_write_side_effect_calls_are_counted(tmp_path: Path) -> None:
    """The evaluation runner derives 副作用次数 from this counter."""
    registry = ToolRegistry()
    registry.register(
        "mutate",
        ToolSpec(required={}, optional={}, handler=lambda _a: {"ok": 1}, side_effect="write", max_output_bytes=1024),
    )
    registry.register(
        "peek",
        ToolSpec(required={}, optional={}, handler=lambda _a: {"ok": 1}, side_effect="read", max_output_bytes=1024),
    )
    assert registry.write_calls() == 0
    registry.call("peek", "{}")
    assert registry.write_calls() == 0, "read 工具不得计入副作用"
    registry.call("mutate", "{}")
    registry.call("mutate", "{}")
    assert registry.write_calls() == 2
    registry.reset_events()
    assert registry.write_calls() == 0
