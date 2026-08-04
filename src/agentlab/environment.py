"""Agent interaction environment: tools + session context + strategy loop."""

from __future__ import annotations

from dataclasses import dataclass

from .longterm import LongTermMemory, build_full_context
from .memory import MemoryState, ShortTermMemory
from .messages import Message
from .reasoning import (
    Planner,
    PlanExecuteStrategy,
    ReActStrategy,
    Reasoner,
    StepExecutor,
    ToolRunner,
    Trace,
)


@dataclass(frozen=True)
class EnvironmentRun:
    task: str
    trace: Trace
    context_messages: int


class AgentEnvironment:
    """Compose tool execution, session context and a strategy loop.

    The environment is deterministic: strategies and tools are injected, and
    session context is assembled from the memory modules.
    """

    def __init__(
        self,
        *,
        tools: ToolRunner,
        reasoner: Reasoner | None = None,
        planner: Planner | None = None,
        step_executor: StepExecutor | None = None,
        strategy: str = "react",
    ) -> None:
        if strategy not in {"react", "plan_execute"}:
            raise ValueError("strategy 必须为 react 或 plan_execute")
        if strategy == "react" and reasoner is None:
            raise ValueError("react strategy 需要 reasoner")
        if strategy == "plan_execute" and (planner is None or step_executor is None):
            raise ValueError("plan_execute strategy 需要 planner 与 step_executor")
        self.tools = tools
        self.strategy = strategy
        self.reasoner = reasoner
        self.planner = planner
        self.step_executor = step_executor

    def build_context(
        self,
        short_term: MemoryState,
        long_term: LongTermMemory,
        query: str,
        *,
        top_k: int = 3,
    ) -> tuple[Message, ...]:
        return build_full_context(short_term, long_term, query, top_k=top_k)

    def run(
        self,
        task: str,
        *,
        short_term: ShortTermMemory | None = None,
        long_term: LongTermMemory | None = None,
        query: str | None = None,
        initial_context: tuple[str, ...] = (),
    ) -> EnvironmentRun:
        if self.strategy == "plan_execute" and (
            short_term is not None or long_term is not None or query is not None or initial_context
        ):
            raise ValueError("plan_execute strategy 尚不支持 memory context")
        if short_term is None and (long_term is not None or query is not None):
            raise ValueError("long_term 与 query 需要 short_term")
        context_provider = None
        message_sink = None
        if short_term is not None:
            short_term.add(Message("user", task))

            def current_context() -> tuple[str, ...]:
                state = short_term.state()
                messages = (
                    build_full_context(state, long_term, query or task)
                    if long_term is not None
                    else state.recent_messages
                )
                return tuple(f"{message.role}: {message.content}" for message in messages)

            def remember(role: str, content: str) -> None:
                short_term.add(Message(role, content))

            context_provider = current_context
            message_sink = remember

        if self.strategy == "react":
            assert self.reasoner is not None
            trace = ReActStrategy(reasoner=self.reasoner, tools=self.tools).run(
                task,
                initial_context=initial_context,
                context_provider=context_provider,
                message_sink=message_sink,
            )
        else:
            assert self.planner is not None and self.step_executor is not None
            trace = PlanExecuteStrategy(planner=self.planner, step_executor=self.step_executor, tools=self.tools).run(task)
        context_messages = len(context_provider()) if context_provider is not None else len(initial_context)
        return EnvironmentRun(task=task, trace=trace, context_messages=context_messages)
