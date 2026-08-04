"""Live reasoner contract tests with an injected fake backend."""

from __future__ import annotations

from agentlab.research.live import LiveResearchReasoner
import pytest


class FakeBackend:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        return self.outputs.pop(0)


def test_live_reasoner_parses_tool_action() -> None:
    backend = FakeBackend(['{"kind":"tool","tool":"list_sources","argument":"{}"}'])
    action = LiveResearchReasoner(backend).decide(
        task="question", context=("system: rule",), available_tools=("list_sources",)
    )
    assert action.kind == "tool" and action.tool == "list_sources"
    assert "untrusted data" in backend.messages[0][0]["content"]
    assert LiveResearchReasoner(backend).token_usage() == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def test_live_reasoner_repairs_once() -> None:
    backend = FakeBackend(["bad", '{"kind":"finish","answer":"{}"}'])
    action = LiveResearchReasoner(backend).decide(task="q", context=(), available_tools=())
    assert action.kind == "finish"
    assert len(backend.messages) == 2


def test_grounded_hybrid_prompt_profile_reaches_provider() -> None:
    backend = FakeBackend(['{"kind":"tool","tool":"list_sources","argument":"{}"}'])
    LiveResearchReasoner(backend, prompt_profile_id="grounded-hybrid-v1").decide(
        task="question",
        context=("system: untrusted",),
        available_tools=("list_sources",),
    )
    prompt = backend.messages[0][0]["content"]
    assert "Search before answering" in prompt
    assert "Cite conflicting sources separately" in prompt
    assert "Refuse when evidence is missing" in prompt


def test_unknown_prompt_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知 prompt profile"):
        LiveResearchReasoner(FakeBackend([]), prompt_profile_id="unknown")
