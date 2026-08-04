"""Reasoning strategies: ReAct vs Plan-and-Execute (deterministic, offline).

Both strategies consume injected, deterministic reasoners/planners so the
comparison is reproducible and never depends on a live model. The comparison
measures structural differences (steps, tool calls, replans), not model quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence


class ToolError(RuntimeError):
    pass


class ToolRunner(Protocol):
    def names(self) -> tuple[str, ...]:
        ...

    def call(self, tool: str, argument: str) -> str:
        ...


class ToolExecutor:
    """Scripted tool behavior; no network, no side effects."""

    def __init__(self, behaviors: Mapping[str, str | Exception]) -> None:
        if not behaviors:
            raise ValueError("至少需要一个工具")
        self._behaviors = dict(behaviors)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._behaviors))

    def call(self, tool: str, argument: str) -> str:
        behavior = self._behaviors.get(tool)
        if behavior is None:
            raise ToolError(f"未知工具: {tool}")
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


@dataclass(frozen=True)
class Action:
    kind: str
    tool: str | None = None
    argument: str | None = None
    answer: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "tool":
            if not self.tool or not self.argument:
                raise ValueError("tool action 需要 tool 与 argument")
        elif self.kind == "finish":
            if not self.answer:
                raise ValueError("finish action 需要 answer")
        else:
            raise ValueError("action.kind 无效")


@dataclass(frozen=True)
class StepRecord:
    step: int
    kind: str
    detail: str


@dataclass(frozen=True)
class Trace:
    strategy: str
    records: tuple[StepRecord, ...]
    success: bool
    answer: str | None = None
    replans: int = 0


class Reasoner(Protocol):
    def decide(self, *, task: str, context: tuple[str, ...], available_tools: tuple[str, ...]) -> Action:
        ...


class Planner(Protocol):
    def plan(self, *, task: str, available_tools: tuple[str, ...]) -> tuple[str, ...]:
        ...

    def replan(self, *, task: str, plan: tuple[str, ...], context: tuple[str, ...]) -> tuple[str, ...]:
        ...


class StepExecutor(Protocol):
    def action_for(self, step: str) -> Action:
        ...


class DeterministicReasoner:
    """Scripted ReAct reasoner: follow a fixed tool sequence, then finish."""

    def __init__(
        self,
        *,
        tool_sequence: Mapping[str, tuple[tuple[str, str], ...]],
        answers: Mapping[str, str],
    ) -> None:
        self.tool_sequence = tool_sequence
        self.answers = answers

    def decide(self, *, task: str, context: tuple[str, ...], available_tools: tuple[str, ...]) -> Action:
        sequence = self.tool_sequence[task]
        observations = [
            item for item in context
            if item.startswith("观察:") or item.startswith("tool: 观察:")
        ]
        if len(observations) < len(sequence):
            tool, argument = sequence[len(observations)]
            return Action(kind="tool", tool=tool, argument=argument)
        return Action(kind="finish", answer=self.answers[task])


class DeterministicPlanner:
    """Scripted planner: fixed steps plus a fixed fallback on replan."""

    def __init__(self, *, steps: Mapping[str, tuple[str, ...]], fallback_step: str) -> None:
        self.steps = steps
        self.fallback_step = fallback_step

    def plan(self, *, task: str, available_tools: tuple[str, ...]) -> tuple[str, ...]:
        return self.steps[task]

    def replan(self, *, task: str, plan: tuple[str, ...], context: tuple[str, ...]) -> tuple[str, ...]:
        return (*plan, self.fallback_step)


class DeterministicStepExecutor:
    """Map a plan step to a concrete tool action."""

    def __init__(self, mapping: Mapping[str, tuple[str, str]]) -> None:
        self.mapping = mapping

    def action_for(self, step: str) -> Action:
        tool, argument = self.mapping[step]
        return Action(kind="tool", tool=tool, argument=argument)


class ReActStrategy:
    def __init__(self, *, reasoner: Reasoner, tools: ToolRunner, max_steps: int = 10) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps 必须为正整数")
        self.reasoner = reasoner
        self.tools = tools
        self.max_steps = max_steps

    def run(
        self,
        task: str,
        *,
        initial_context: tuple[str, ...] = (),
        context_provider: Callable[[], tuple[str, ...]] | None = None,
        message_sink: Callable[[str, str], None] | None = None,
    ) -> Trace:
        if (context_provider is None) != (message_sink is None):
            raise ValueError("context_provider 与 message_sink 必须成对提供")
        context: list[str] = [f"任务: {task}", *initial_context]
        records: list[StepRecord] = []
        for step in range(1, self.max_steps + 1):
            decision_context = (
                (*initial_context, *context_provider())
                if context_provider is not None
                else tuple(context)
            )
            action = self.reasoner.decide(
                task=task,
                context=decision_context,
                available_tools=self.tools.names(),
            )
            if action.kind == "finish":
                assert action.answer is not None
                records.append(StepRecord(step, "finish", action.answer))
                if message_sink is not None:
                    message_sink("assistant", action.answer)
                return Trace(strategy="react", records=tuple(records), success=True, answer=action.answer)
            assert action.tool is not None and action.argument is not None
            records.append(StepRecord(step, "tool", f"{action.tool}({action.argument})"))
            if message_sink is not None:
                message_sink("assistant", f"工具调用: {action.tool}({action.argument})")
            try:
                result = self.tools.call(action.tool, action.argument)
            except ToolError as exc:
                result = f"error: {exc}"
            context.append(f"观察: {result}")
            if message_sink is not None:
                message_sink("tool", f"观察: {result}")
            records.append(StepRecord(step, "observation", result))
        return Trace(strategy="react", records=tuple(records), success=False)


class PlanExecuteStrategy:
    def __init__(
        self,
        *,
        planner: Planner,
        step_executor: StepExecutor,
        tools: ToolRunner,
        max_replans: int = 2,
    ) -> None:
        if max_replans < 0:
            raise ValueError("max_replans 必须非负")
        self.planner = planner
        self.step_executor = step_executor
        self.tools = tools
        self.max_replans = max_replans

    def run(self, task: str) -> Trace:
        plan = self.planner.plan(task=task, available_tools=self.tools.names())
        records: list[StepRecord] = []
        for index, step in enumerate(plan, start=1):
            records.append(StepRecord(index, "plan", step))
        replans = 0
        step_number = len(plan)
        pending = list(plan)
        answers: list[str] = []
        while pending:
            step = pending.pop(0)
            step_number += 1
            action = self.step_executor.action_for(step)
            assert action.tool is not None and action.argument is not None
            records.append(StepRecord(step_number, "tool", f"{action.tool}({action.argument})"))
            try:
                result = self.tools.call(action.tool, action.argument)
                records.append(StepRecord(step_number, "observation", result))
                answers.append(result)
            except ToolError as exc:
                records.append(StepRecord(step_number, "observation", f"error: {exc}"))
                if replans >= self.max_replans:
                    return Trace(strategy="plan_execute", records=tuple(records), success=False, replans=replans)
                replans += 1
                new_plan = self.planner.replan(
                    task=task,
                    plan=tuple(pending),
                    context=tuple(record.detail for record in records),
                )
                for extra in new_plan[len(pending):]:
                    pending.append(extra)
                    step_number += 1
                    records.append(StepRecord(step_number, "plan", extra))
        answer = "；".join(answers)
        return Trace(strategy="plan_execute", records=tuple(records), success=True, answer=answer, replans=replans)


def run_comparison(
    tasks: Sequence[str],
    *,
    reasoner: Reasoner,
    planner: Planner,
    step_executor: StepExecutor,
    tools: ToolRunner,
) -> tuple[Trace, ...]:
    """Run both strategies over the fixed task set; fully deterministic."""
    react = ReActStrategy(reasoner=reasoner, tools=tools)
    plan = PlanExecuteStrategy(planner=planner, step_executor=step_executor, tools=tools)
    traces: list[Trace] = []
    for task in tasks:
        traces.append(react.run(task))
        traces.append(plan.run(task))
    return tuple(traces)


def render_comparison(traces: Sequence[Trace]) -> str:
    lines = ["strategy | steps | tool_calls | replans | success"]
    for trace in traces:
        tool_calls = sum(1 for record in trace.records if record.kind == "tool")
        lines.append(
            f"{trace.strategy} | {len(trace.records)} | {tool_calls} | {trace.replans} | {trace.success}"
        )
    return "\n".join(lines)
