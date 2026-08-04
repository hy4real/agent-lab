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

    registry.register("fail", ToolSpec(required={}, optional={}, handler=fail))
    with pytest.raises(ToolError, match="^tool_failure$"):
        registry.call("fail", "{}")
