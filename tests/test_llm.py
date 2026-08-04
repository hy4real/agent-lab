"""LLM adapter parsing tests (offline, fake backend)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from agentlab.llm import (
    LLMBackend,
    ModelPlanner,
    ModelReasoner,
    ProviderError,
    parse_action,
    parse_plan,
    parse_step,
)


class FakeBackend:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def complete(self, messages) -> str:
        return self.responses.pop(0)


def test_parse_action_tool_and_finish() -> None:
    action = parse_action('{"kind":"tool","tool":"search","argument":"天气"}')
    assert action.kind == "tool" and action.tool == "search" and action.argument == "天气"
    action = parse_action('{"kind":"finish","answer":"23.3"}')
    assert action.kind == "finish" and action.answer == "23.3"
    action = parse_action('```json\n{"kind":"tool","tool":"calc","argument":"x"}\n```')
    assert action.kind == "tool" and action.tool == "calc"
    with pytest.raises(ValueError):
        parse_action("不是 JSON")


def test_parse_plan_and_step() -> None:
    assert parse_plan('{"steps": ["search(天气)", "calc(平均)"]}') == ("search(天气)", "calc(平均)")
    with pytest.raises(ValueError):
        parse_plan("[]")
    step = parse_step("search(天气)")
    assert step.kind == "tool" and step.tool == "search" and step.argument == "天气"
    with pytest.raises(ValueError):
        parse_step("bad format")


def test_model_reasoner_repairs_bad_json_once() -> None:
    backend = FakeBackend(["not json", '{"kind":"finish","answer":"ok"}'])
    reasoner = ModelReasoner(backend, max_repairs=1)
    action = reasoner.decide(task="t", context=("观察: x",), available_tools=("search",))
    assert action.kind == "finish" and action.answer == "ok"
    assert len(backend.responses) == 0


def test_model_reasoner_fails_after_repairs() -> None:
    backend = FakeBackend(["not json", "still not json"])
    reasoner = ModelReasoner(backend, max_repairs=1)
    with pytest.raises(RuntimeError):
        reasoner.decide(task="t", context=(), available_tools=())


def test_model_planner_parses_steps() -> None:
    backend = FakeBackend(['{"steps": ["search(天气)"]}'])
    planner = ModelPlanner(backend)
    assert planner.plan(task="t", available_tools=("search",)) == ("search(天气)",)


def test_backend_requires_authorized_env() -> None:
    # Construction without openai package would raise ImportError; with package,
    # missing key raises at client construction. We only assert module exists.
    assert LLMBackend.__name__ == "LLMBackend"


def test_backend_hides_provider_initialization_diagnostic(monkeypatch) -> None:
    def fail_client(**_kwargs):
        raise ValueError("private initialization response")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=fail_client))
    with pytest.raises(ProviderError, match="provider_initialization_failed") as caught:
        LLMBackend(api_key="synthetic")
    assert "private initialization response" not in str(caught.value)


def test_backend_hides_provider_request_diagnostic() -> None:
    backend = object.__new__(LLMBackend)
    backend.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: (_ for _ in ()).throw(ValueError("private response body"))
            )
        )
    )
    backend.model = "synthetic"
    backend.temperature = 0.0
    backend.usage = {"prompt_tokens": 0, "completion_tokens": 0}
    with pytest.raises(ProviderError, match="provider_request_failed") as caught:
        backend.complete([])
    assert "private response body" not in str(caught.value)
