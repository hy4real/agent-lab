"""Agent interaction environment contract tests (offline, deterministic)."""

from __future__ import annotations

import pytest

from agentlab.demo import run_environment_demo
from agentlab.environment import AgentEnvironment
from agentlab.longterm import LongTermMemory
from agentlab.memory import MemoryState, ShortTermMemory
from agentlab.messages import Message
from agentlab.reasoning import (
    DeterministicPlanner,
    DeterministicReasoner,
    DeterministicStepExecutor,
    ToolExecutor,
)


def _environment() -> AgentEnvironment:
    tools = ToolExecutor({"search": "20, 24, 26", "calc": "23.3"})
    reasoner = DeterministicReasoner(
        tool_sequence={"task-a": (("search", "天气"), ("calc", "平均(20,24,26)"))},
        answers={"task-a": "平均温度 23.3"},
    )
    planner = DeterministicPlanner(
        steps={"task-a": ("search 查询天气数据", "calc 计算平均温度")},
        fallback_step="cache 读取缓存",
    )
    executor = DeterministicStepExecutor({
        "search 查询天气数据": ("search", "天气"),
        "calc 计算平均温度": ("calc", "平均(20,24,26)"),
    })
    return AgentEnvironment(tools=tools, reasoner=reasoner, planner=planner, step_executor=executor, strategy="react")


def test_environment_runs_strategy_and_reports() -> None:
    run = _environment().run("task-a")
    assert run.trace.success is True
    assert run.trace.answer == "平均温度 23.3"
    assert run.context_messages == 0


def test_environment_plan_strategy() -> None:
    environment = _environment()
    run = AgentEnvironment(
        tools=environment.tools,
        reasoner=environment.reasoner,
        planner=environment.planner,
        step_executor=environment.step_executor,
        strategy="plan_execute",
    ).run("task-a")
    assert run.trace.success is True


def test_environment_requires_only_selected_strategy_dependencies() -> None:
    environment = _environment()
    react = AgentEnvironment(tools=environment.tools, reasoner=environment.reasoner)
    assert react.run("task-a").trace.success is True
    planned = AgentEnvironment(
        tools=environment.tools,
        planner=environment.planner,
        step_executor=environment.step_executor,
        strategy="plan_execute",
    )
    assert planned.run("task-a").trace.success is True


def test_plan_execute_rejects_memory_context() -> None:
    environment = _environment()
    planned = AgentEnvironment(
        tools=environment.tools,
        planner=environment.planner,
        step_executor=environment.step_executor,
        strategy="plan_execute",
    )
    with pytest.raises(ValueError, match="不支持 memory"):
        planned.run("task-a", short_term=ShortTermMemory())


def test_environment_assembles_full_context(tmp_path) -> None:
    long_term = LongTermMemory(state_path=tmp_path / "longterm.json", clock=lambda: "2026-07-31T00:00:00+00:00")
    long_term.add("缓存方案已定")
    short_term = MemoryState(summary="摘要", recent_messages=(Message("user", "最近消息"),), budget_tokens=100)
    context = _environment().build_context(short_term, long_term, "缓存", top_k=1)
    assert context[0].role == "system"
    assert context[-1] == Message("user", "最近消息")


def test_environment_feeds_and_updates_short_term_memory(tmp_path) -> None:
    short_term = ShortTermMemory(budget_tokens=1000)
    long_term = LongTermMemory(state_path=tmp_path / "longterm.json")
    long_term.add("天气任务使用缓存")
    run = _environment().run(
        "task-a",
        short_term=short_term,
        long_term=long_term,
        query="天气",
        initial_context=("系统规则",),
    )
    contents = tuple(message.content for message in short_term.state().recent_messages)
    assert contents[0] == "task-a"
    assert any(content.startswith("工具调用:") for content in contents)
    assert any(content.startswith("观察:") for content in contents)
    assert contents[-1] == "平均温度 23.3"
    assert run.context_messages == len(short_term.state().recent_messages) + 1


def test_environment_rejects_unknown_strategy() -> None:
    environment = _environment()
    with pytest.raises(ValueError):
        AgentEnvironment(
            tools=environment.tools,
            reasoner=environment.reasoner,
            planner=environment.planner,
            step_executor=environment.step_executor,
            strategy="explode",
        )


def test_environment_demo_is_reproducible() -> None:
    assert run_environment_demo() == run_environment_demo()
    assert "success=True" in run_environment_demo()
