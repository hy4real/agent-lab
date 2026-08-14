"""Deterministic offline demos for memory modules."""

from __future__ import annotations

import tempfile
from itertools import cycle
from pathlib import Path

from .longterm import LongTermMemory, inject_long_term
from .memory import ShortTermMemory, inject_memory
from .messages import Message
from .evaluation import EvalCase, EvalRunner, RegressionSuite, aggregate_runs, render_report
from .environment import AgentEnvironment
from .retrieval import render_experiment, run_chunking_experiment
from .reasoning import (
    DeterministicPlanner,
    DeterministicReasoner,
    DeterministicStepExecutor,
    ToolExecutor,
    render_comparison,
    run_comparison,
)
from .resilience import (
    CircuitBreaker,
    DurableTaskQueue,
    RateLimiter,
    Task,
    TTLCache,
    WorkerPool,
)
from .summarize import DeterministicSummarizer


DEMO_MESSAGES = (
    Message("user", "帮我设计一个带记忆的 Agent，第一步做什么？"),
    Message("assistant", "先定义消息契约与 token 预算，再实现窗口管理。"),
    Message("user", "窗口满了之后怎么处理旧消息？"),
    Message("assistant", "把最旧的消息折叠进运行摘要，保留最近可见窗口。"),
    Message("tool", "memory.add 调用返回折叠 1 条，摘要长度 96 字符。"),
    Message("user", "摘要会累积吗？"),
    Message("assistant", "会；每次折叠都基于已有摘要增量追加。"),
    Message("tool", "memory.add 调用返回折叠 2 条，摘要长度 188 字符。"),
)


def run_demo(*, budget_tokens: int = 32) -> str:
    """Run the fixed conversation and render every memory state deterministically."""
    memory = ShortTermMemory(budget_tokens=budget_tokens, summarizer=DeterministicSummarizer())
    lines: list[str] = []
    for index, message in enumerate(DEMO_MESSAGES, start=1):
        state = memory.add(message)
        lines.append(f"turn {index}: {message.role} {message.content!r}")
        lines.append(f"  summary={state.summary!r}")
        lines.append(f"  recent={[item.content for item in state.recent_messages]!r}")
        lines.append(f"  estimated_tokens={state.estimated_tokens()}")
        lines.append(f"  injected={[item.content for item in inject_memory(state)]!r}")
    return "\n".join(lines)


def run_longterm_demo(*, top_k: int = 2) -> str:
    """Fixed proposal/confirmation flow and bounded recall."""
    state_dir = Path(tempfile.mkdtemp(prefix="agentlab-longterm-demo."))
    state_file = state_dir / "longterm.json"
    memory = LongTermMemory(state_path=state_file, clock=lambda: "2026-07-31T00:00:00+00:00")
    try:
        entries = (
            "项目 Alpha 使用异步队列处理任务",
            "项目 Beta 实现 LRU 缓存并压测命中率",
            "会议结论：优先可离线复现的评测",
        )
        lines: list[str] = []
        revision = 0
        for text in entries:
            entry = memory.propose(
                text,
                kind="note",
                scope="demo",
                origin="manual",
                expected_revision=revision,
            )
            revision += 1
            lines.append(f"propose: {entry.id} status={entry.status}")
            entry = memory.confirm(entry.id, scope="demo", expected_revision=revision)
            revision += 1
            lines.append(f"confirm: {entry.id} status={entry.status}")
        query = "缓存 离线 评测"
        lines.append(f"query: {query!r}")
        for entry in memory.search(query, scope="demo", top_k=top_k):
            lines.append(f"retrieved: {entry.id} {entry.text!r}")
        for message in inject_long_term(memory, query, scope="demo", top_k=top_k):
            lines.append(f"injected: {message.role} {message.content!r}")
        return "\n".join(lines)
    finally:
        try:
            state_file.unlink()
        except FileNotFoundError:
            pass
        try:
            state_file.with_name(f"{state_file.name}.lock").unlink()
        except FileNotFoundError:
            pass
        state_dir.rmdir()


def run_reasoning_demo() -> str:
    """Fixed tasks, fixed scripted backends: reproducible strategy comparison."""
    tools = ToolExecutor({"search": "20, 24, 26", "calc": "23.3", "fetch": "raw data"})
    reasoner = DeterministicReasoner(
        tool_sequence={
            "task-a": (("search", "天气"), ("calc", "平均(20,24,26)")),
            "task-b": (("fetch", "数据"), ("calc", "汇总(raw)")),
        },
        answers={"task-a": "平均温度 23.3", "task-b": "汇总完成"},
    )
    planner = DeterministicPlanner(
        steps={
            "task-a": ("search 查询天气数据", "calc 计算平均温度"),
            "task-b": ("fetch 拉取数据", "calc 汇总数据"),
        },
        fallback_step="cache 读取缓存",
    )
    step_executor = DeterministicStepExecutor({
        "search 查询天气数据": ("search", "天气"),
        "calc 计算平均温度": ("calc", "平均(20,24,26)"),
        "fetch 拉取数据": ("fetch", "数据"),
        "calc 汇总数据": ("calc", "汇总(raw)"),
    })
    traces = run_comparison(("task-a", "task-b"), reasoner=reasoner, planner=planner, step_executor=step_executor, tools=tools)
    return render_comparison(traces)


def run_resilience_demo() -> str:
    """Fixed tasks and clocks: reproducible queue, cache and breaker behavior."""
    import asyncio
    import tempfile

    state_dir = Path(tempfile.mkdtemp(prefix="agentlab-resilience-demo."))
    lines: list[str] = []
    try:
        queue = DurableTaskQueue(state_path=state_dir / "journal.jsonl", clock=lambda: "2026-07-31T00:00:00+00:00")
        queue.add(Task(id="a", payload="low", priority=2))
        queue.add(Task(id="b", payload="high", priority=1))
        stats = asyncio.run(WorkerPool(workers=2, processor=lambda task: "ok").drain(queue, now="2026-07-31T00:00:00+00:00"))
        lines.append(f"worker stats: {stats}")
        cache = TTLCache(capacity=2, clock=lambda: "2026-07-31T00:00:00+00:00")
        cache.put("k1", "v1")
        cache.put("k2", "v2")
        cache.put("k3", "v3")
        lines.append(f"cache stats: {cache.stats()}")
        breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=10, clock=lambda: 0.0)

        def failing() -> str:
            raise RuntimeError("boom")

        try:
            breaker.call(failing)
        except RuntimeError:
            pass
        try:
            breaker.call(failing)
        except RuntimeError:
            pass
        lines.append(f"breaker state: {breaker.state()}")
        limiter = RateLimiter(max_calls=1, window_seconds=10, clock=lambda: 0.0)
        lines.append(f"rate limiter: allow={limiter.allow()}, allow={limiter.allow()}")
        return "\n".join(lines)
    finally:
        for path in state_dir.glob("*"):
            path.unlink()
        state_dir.rmdir()


def run_eval_demo() -> str:
    """Fixed cases and clocks: reproducible regression and observability."""
    cases = (
        EvalCase(id="case-1", task="查询天气", expected_result="晴", tags=("weather",)),
        EvalCase(id="case-2", task="计算平均温度", expected_result="23", tags=("math",)),
        EvalCase(id="case-3", task="生成摘要", expected_result="摘要", tags=("summarize",)),
    )
    times = cycle([0.0, 1.0, 10.0, 11.0, 20.0, 21.0])

    def execute(case: EvalCase) -> str:
        return "结果：21" if case.id == "case-2" else case.expected_result

    runner = EvalRunner(execute=execute, clock=lambda: next(times))
    results = runner.run(cases)
    suite = RegressionSuite(cases=cases)
    baseline = suite.freeze(runner)
    report = suite.run(runner)
    lines = [
        render_report(results, boundary_note="offline baseline，不作为模型质量结论"),
        f"regression: passed={report.passed} failed={report.failed} baseline_match={report.baseline_match}",
        f"baseline_hash={baseline[:16]}",
        f"observability: {aggregate_runs((results, results))}",
    ]
    return "\n".join(lines)


def run_retrieval_demo() -> str:
    """Fixed synthetic corpus and queries: reproducible chunking experiment."""
    documents = (
        "本文介绍缓存策略显著提升命中率并降低延迟，缓存采用 LRU 策略并做离线评测。",
        "本文介绍异步队列与任务调度，队列支持优先级并支持延迟执行。",
        "本文介绍熔断器与降级策略，用于高可用系统并支持故障注入测试。",
    )
    queries = (
        ("缓存 命中率 离线评测", "缓存策略显著提升命中率并降低延迟"),
        ("异步 队列 调度", "异步队列与任务调度"),
        ("熔断 降级 高可用", "熔断器与降级策略，用于高可用系统"),
    )
    report = run_chunking_experiment(documents=documents, queries=queries, chunk_sizes=(6, 12, 24), k=3)
    return render_experiment(report)


def run_environment_demo() -> str:
    """Fixed task and backends: reproducible environment run."""
    from .reasoning import (
        DeterministicPlanner,
        DeterministicReasoner,
        DeterministicStepExecutor,
        ToolExecutor,
    )

    tools = ToolExecutor({"search": "20, 24, 26", "calc": "23.3"})
    reasoner = DeterministicReasoner(
        tool_sequence={"task-a": (("search", "天气"), ("calc", "平均(20,24,26)"))},
        answers={"task-a": "平均温度 23.3"},
    )
    planner = DeterministicPlanner(steps={"task-a": ("search 查询天气数据", "calc 计算平均温度")}, fallback_step="cache 读取缓存")
    executor = DeterministicStepExecutor({"search 查询天气数据": ("search", "天气"), "calc 计算平均温度": ("calc", "平均(20,24,26)")})
    environment = AgentEnvironment(tools=tools, reasoner=reasoner, planner=planner, step_executor=executor, strategy="react")
    run = environment.run("task-a")
    return f"environment run: task={run.task!r} success={run.trace.success} answer={run.trace.answer!r} context_messages={run.context_messages}"


if __name__ == "__main__":
    print(run_demo())
    print("---")
    print(run_longterm_demo())
    print("---")
    print(run_reasoning_demo())
    print("---")
    print(run_resilience_demo())
    print("---")
    print(run_eval_demo())
    print("---")
    print(run_retrieval_demo())
    print("---")
    print(run_environment_demo())
