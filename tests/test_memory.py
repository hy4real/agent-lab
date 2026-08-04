"""Short-term memory contract tests (fully offline, deterministic)."""

from __future__ import annotations

import pytest

from agentlab.demo import run_demo
from agentlab.memory import MemoryState, ShortTermMemory, inject_memory
from agentlab.messages import Message, estimate_tokens
from agentlab.summarize import DeterministicSummarizer


def _messages(*contents: str) -> list[Message]:
    return [Message("user", content) for content in contents]


def test_under_budget_keeps_all_messages() -> None:
    memory = ShortTermMemory(budget_tokens=10_000)
    for message in _messages("alpha", "beta", "gamma"):
        memory.add(message)
    state = memory.state()
    assert state.summary == ""
    assert [item.content for item in state.recent_messages] == ["alpha", "beta", "gamma"]


def test_over_budget_folds_oldest_into_summary() -> None:
    memory = ShortTermMemory(budget_tokens=25, summarizer=DeterministicSummarizer())
    messages = _messages("x" * 40, "y" * 40, "z" * 40)
    for message in messages:
        memory.add(message)
    state = memory.state()
    assert len(state.recent_messages) == 1
    assert state.recent_messages[0].content == "z" * 40
    assert "[user]" in state.summary


def test_summary_accumulates_across_turns() -> None:
    memory = ShortTermMemory(budget_tokens=35, summarizer=DeterministicSummarizer())
    for content in ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40):
        memory.add(Message("user", content))
    state = memory.state()
    assert state.summary.count("[user]") >= 2
    assert "a" in state.summary and "b" in state.summary


def test_single_oversized_message_is_kept() -> None:
    memory = ShortTermMemory(budget_tokens=20, summarizer=DeterministicSummarizer())
    state = memory.add(Message("user", "x" * 200))
    assert len(state.recent_messages) == 1
    assert state.summary == ""


def test_injection_contains_summary_then_recent_messages() -> None:
    state = MemoryState(
        summary="早期对话已折叠",
        recent_messages=(Message("user", "b"), Message("assistant", "c")),
        budget_tokens=100,
    )
    injected = inject_memory(state)
    assert injected[0].role == "system"
    assert "摘要" in injected[0].content
    assert injected[1:] == state.recent_messages


def test_roles_are_preserved() -> None:
    memory = ShortTermMemory(budget_tokens=10_000)
    memory.add(Message("user", "hi"))
    memory.add(Message("assistant", "hello"))
    memory.add(Message("tool", "ok"))
    assert [item.role for item in memory.state().recent_messages] == ["user", "assistant", "tool"]


def test_memory_is_deterministic() -> None:
    def build() -> str:
        memory = ShortTermMemory(budget_tokens=25, summarizer=DeterministicSummarizer())
        for content in ("p" * 40, "q" * 40, "r" * 40, "s" * 40):
            memory.add(Message("user", content))
        return repr(memory.state())

    assert build() == build()


def test_demo_is_reproducible() -> None:
    assert run_demo() == run_demo()
    assert "摘要" in run_demo()
    assert run_demo().count("turn ") == 8


def test_estimate_tokens_is_ceiling_and_strict() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd", chars_per_token=4) == 1
    assert estimate_tokens("abcde", chars_per_token=4) == 2
    with pytest.raises(ValueError):
        estimate_tokens("x", chars_per_token=0)


def test_message_validation() -> None:
    with pytest.raises(ValueError):
        Message("narrator", "bad role")
    with pytest.raises(ValueError):
        Message("user", "  ")


def test_invalid_budget_rejected() -> None:
    with pytest.raises(ValueError):
        ShortTermMemory(budget_tokens=0)
