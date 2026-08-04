"""Authorized live-model evaluation runner (explicit boundary)."""

from __future__ import annotations

from ..llm import LLMBackend, ModelPlanner, ModelReasoner, parse_step
from ..reasoning import (
    PlanExecuteStrategy,
    ReActStrategy,
    ToolExecutor,
    Trace,
    render_comparison,
)


REAL_TASK = "请用 search 工具查询三个城市的温度，再用 calc 工具计算平均温度，最后回答平均温度是多少。"


def run_real_eval(*, backend: LLMBackend | None = None) -> dict[str, object]:
    """Run one tool-using task under both strategies with a real model."""
    backend = backend or LLMBackend()
    tools = ToolExecutor({"search": "20, 24, 26", "calc": "23.3"})
    reasoner = ModelReasoner(backend)
    planner = ModelPlanner(backend)

    react = ReActStrategy(reasoner=reasoner, tools=tools, max_steps=8)
    plan = PlanExecuteStrategy(
        planner=planner,
        step_executor=_StepExecutor(),
        tools=tools,
        max_replans=1,
    )
    react_trace = react.run(REAL_TASK)
    plan_trace = plan.run(REAL_TASK)
    traces = (react_trace, plan_trace)
    return {
        "task": REAL_TASK,
        "traces": [trace_to_dict(trace) for trace in traces],
        "comparison": render_comparison(traces),
        "usage": dict(backend.usage),
        "boundary": "单次真实模型运行；工具为脚本化确定性工具；结果不表述模型能力、市场或外部结果",
    }


class _StepExecutor:
    def action_for(self, step: str):
        return parse_step(step)


def trace_to_dict(trace: Trace) -> dict[str, object]:
    return {
        "strategy": trace.strategy,
        "success": trace.success,
        "answer": trace.answer,
        "replans": trace.replans,
        "records": [(record.step, record.kind, record.detail) for record in trace.records],
    }
