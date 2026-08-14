"""Reasoning strategy contract tests (fully offline, deterministic)."""

from __future__ import annotations

import pytest

from agentlab.demo import run_reasoning_demo
from agentlab.reasoning import (
    Action,
    DeterministicPlanner,
    DeterministicReasoner,
    DeterministicStepExecutor,
    PlanExecuteStrategy,
    ReActStrategy,
    ToolError,
    ToolExecutor,
    ToolObservation,
    render_comparison,
    run_comparison,
)


def _tools() -> ToolExecutor:
    return ToolExecutor({"search": "20, 24, 26", "calc": "23.3"})


def _reasoner() -> DeterministicReasoner:
    return DeterministicReasoner(
        tool_sequence={"task-a": (("search", "天气"), ("calc", "平均(20,24,26)"))},
        answers={"task-a": "平均温度 23.3"},
    )


def _planner() -> DeterministicPlanner:
    return DeterministicPlanner(
        steps={"task-a": ("search 查询天气数据", "calc 计算平均温度")},
        fallback_step="cache 读取缓存",
    )


def _step_executor() -> DeterministicStepExecutor:
    return DeterministicStepExecutor({
        "search 查询天气数据": ("search", "天气"),
        "calc 计算平均温度": ("calc", "平均(20,24,26)"),
    })


def test_react_runs_tool_sequence_then_finishes() -> None:
    trace = ReActStrategy(reasoner=_reasoner(), tools=_tools()).run("task-a")
    assert trace.success is True
    assert trace.answer == "平均温度 23.3"
    assert sum(1 for record in trace.records if record.kind == "tool") == 2
    assert trace.records[-1].kind == "finish"


def test_react_uses_dynamic_context_and_message_sink() -> None:
    contexts: list[tuple[str, ...]] = []
    messages: list[tuple[str, str]] = []

    class RecordingReasoner:
        def decide(self, *, task, context, available_tools):
            contexts.append(context)
            if len(contexts) == 1:
                return Action(kind="tool", tool="search", argument="天气")
            return Action(kind="finish", answer="完成")

    dynamic = ["user: task-a"]

    def sink(role: str, content: str) -> None:
        messages.append((role, content))
        dynamic.append(f"{role}: {content}")

    trace = ReActStrategy(reasoner=RecordingReasoner(), tools=_tools()).run(
        "task-a",
        initial_context=("system: 固定规则",),
        context_provider=lambda: tuple(dynamic),
        message_sink=sink,
    )
    assert trace.success is True
    assert "system: 固定规则" in contexts[0]
    assert any("观察:" in item for item in contexts[1])
    assert sum("观察:" in item for item in contexts[1]) == 1
    assert messages[-1] == ("assistant", "完成")


def test_react_requires_dynamic_context_and_sink_together() -> None:
    with pytest.raises(ValueError, match="成对"):
        ReActStrategy(reasoner=_reasoner(), tools=_tools()).run(
            "task-a", context_provider=lambda: ()
        )


def test_plan_execute_emits_plan_then_executes() -> None:
    trace = PlanExecuteStrategy(planner=_planner(), step_executor=_step_executor(), tools=_tools()).run("task-a")
    assert trace.success is True
    assert "23.3" in trace.answer
    assert sum(1 for record in trace.records if record.kind == "plan") == 2
    assert sum(1 for record in trace.records if record.kind == "tool") == 2
    assert trace.replans == 0


def test_plan_execute_replans_on_tool_failure() -> None:
    tools = ToolExecutor({"fetch": ToolError("服务不可用"), "cache": "cached value"})
    planner = DeterministicPlanner(steps={"task-b": ("fetch 拉取数据",)}, fallback_step="cache 读取缓存")
    executor = DeterministicStepExecutor({"fetch 拉取数据": ("fetch", "data"), "cache 读取缓存": ("cache", "key")})
    trace = PlanExecuteStrategy(planner=planner, step_executor=executor, tools=tools).run("task-b")
    assert trace.replans == 1
    assert trace.success is True
    assert "error: 服务不可用" in "".join(record.detail for record in trace.records)
    assert any(record.detail == "cache(key)" for record in trace.records if record.kind == "tool")


def test_react_survives_tool_error_without_crashing() -> None:
    tools = ToolExecutor({"fetch": ToolError("服务不可用")})
    reasoner = DeterministicReasoner(tool_sequence={"task-c": (("fetch", "data"),)}, answers={"task-c": "降级回答"})
    trace = ReActStrategy(reasoner=reasoner, tools=tools).run("task-c")
    assert trace.success is True
    assert any("error:" in record.detail for record in trace.records if record.kind == "observation")


def test_react_uses_safe_trace_detail_for_sensitive_tool_observation() -> None:
    class SensitiveTools:
        def names(self):
            return ("memory_search",)

        def call(self, tool, argument):
            return ToolObservation("private result", "memory_search:ok:1")

    reasoner = DeterministicReasoner(
        tool_sequence={"task": (("memory_search", "private query"),)},
        answers={"task": "done"},
    )
    trace = ReActStrategy(reasoner=reasoner, tools=SensitiveTools()).run("task")
    trace_text = "\n".join(record.detail for record in trace.records)
    assert "private query" not in trace_text
    assert "private result" not in trace_text
    assert "memory_search:ok:1" in trace_text


def test_max_steps_enforced() -> None:
    reasoner = DeterministicReasoner(
        tool_sequence={"task-d": tuple(("search", "x") for _ in range(50))},
        answers={"task-d": "never"},
    )
    trace = ReActStrategy(reasoner=reasoner, tools=_tools(), max_steps=3).run("task-d")
    assert trace.success is False
    assert len(trace.records) <= 6


def test_comparison_is_deterministic() -> None:
    def build():
        return run_comparison(("task-a",), reasoner=_reasoner(), planner=_planner(), step_executor=_step_executor(), tools=_tools())

    assert build() == build()


def test_render_comparison_contains_rows() -> None:
    traces = run_comparison(("task-a",), reasoner=_reasoner(), planner=_planner(), step_executor=_step_executor(), tools=_tools())
    rendered = render_comparison(traces)
    assert "strategy" in rendered and "react" in rendered and "plan_execute" in rendered


def test_reasoning_demo_is_reproducible() -> None:
    assert run_reasoning_demo() == run_reasoning_demo()
    assert "plan_execute" in run_reasoning_demo()


def test_action_validation() -> None:
    with pytest.raises(ValueError):
        Action(kind="tool")
    with pytest.raises(ValueError):
        Action(kind="finish", answer=None)
    with pytest.raises(ValueError):
        Action(kind="explode")
