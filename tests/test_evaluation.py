"""Evaluation harness contract tests (fully offline, deterministic)."""

from __future__ import annotations

from agentlab.demo import run_eval_demo
from agentlab.evaluation import (
    EvalCase,
    EvalRunner,
    RegressionSuite,
    aggregate,
    aggregate_runs,
    render_observability,
    render_report,
)
from agentlab.evaluation.real import run_real_eval


def _cases() -> tuple[EvalCase, ...]:
    return (
        EvalCase(id="case-1", task="查询天气", expected_result="晴", tags=("weather",)),
        EvalCase(id="case-2", task="计算平均温度", expected_result="23", tags=("math",)),
        EvalCase(id="case-3", task="生成摘要", expected_result="摘要", tags=("summarize",)),
    )


def _runner() -> EvalRunner:
    times = iter([0.0, 1.0, 10.0, 11.0, 20.0, 21.0])

    def execute(case: EvalCase) -> str:
        if case.id == "case-2":
            return "结果：21"
        return case.expected_result

    return EvalRunner(execute=execute, clock=lambda: next(times))


def test_runner_scores_quality_and_usage() -> None:
    results = _runner().run(_cases())
    assert [result.ok for result in results] == [True, False, True]
    assert [result.quality_score for result in results] == [1.0, 0.0, 1.0]
    assert all(result.completion_seconds == 1.0 for result in results)
    assert all(result.tokens_used > 0 for result in results)


def test_aggregate_metrics() -> None:
    metrics = aggregate(_runner().run(_cases()))
    assert metrics["total"] == 3
    assert metrics["ok"] == 2
    assert metrics["success_rate"] == 2 / 3
    assert metrics["avg_quality"] == 2 / 3


def test_regression_suite_pass_fail_and_baseline() -> None:
    suite = RegressionSuite(cases=_cases())
    baseline = suite.freeze(_runner())
    assert isinstance(baseline, str) and len(baseline) == 64
    report = suite.run(_runner())
    assert report.passed == 2 and report.failed == 1
    assert report.baseline_match is None
    tracked = RegressionSuite(cases=_cases(), baseline_hash=baseline)
    assert tracked.run(_runner()).baseline_match is True
    wrong = RegressionSuite(cases=_cases(), baseline_hash="0" * 64)
    assert wrong.run(_runner()).baseline_match is False


def test_observability_aggregation_and_boundary() -> None:
    results = _runner().run(_cases())
    observed = aggregate_runs((results, results))
    assert observed["runs"] == 2
    assert observed["total_cases"] == 6
    assert observed["success_rate"] == 2 / 3
    assert observed["per_tag_success_rate"] == {"math": 0.0, "summarize": 1.0, "weather": 1.0}
    assert "任务完成率代理业务转化" in observed["conversion_proxy_boundary"]
    assert "业务转化" in render_observability((results,))


def test_render_report_contains_rows() -> None:
    rendered = render_report(_runner().run(_cases()), boundary_note="offline baseline")
    assert "case-1" in rendered and "aggregate" in rendered and "boundary" in rendered


def test_eval_demo_is_reproducible() -> None:
    assert run_eval_demo() == run_eval_demo()
    assert "success_rate" in run_eval_demo()


def test_real_eval_adapter_with_fake_backend() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.outputs = [
                '{"kind":"tool","tool":"search","argument":"weather"}',
                '{"kind":"tool","tool":"calc","argument":"average"}',
                '{"kind":"finish","answer":"23.3"}',
                '{"steps":["search(weather)","calc(average)"]}',
            ]
            self.usage = {"prompt_tokens": 10, "completion_tokens": 4}

        def complete(self, messages) -> str:
            return self.outputs.pop(0)

    report = run_real_eval(backend=FakeBackend())
    assert [trace["success"] for trace in report["traces"]] == [True, True]
    assert report["usage"] == {"prompt_tokens": 10, "completion_tokens": 4}
    assert "不表述模型能力" in report["boundary"]
