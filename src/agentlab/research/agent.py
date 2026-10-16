"""Research facade over the existing AgentEnvironment and ReAct loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..environment import AgentEnvironment
from ..longterm import LongTermMemory
from ..memory import ShortTermMemory
from ..messages import estimate_tokens
from ..reasoning import Reasoner
from .artifacts import ResearchAnswer, ResearchValidationError
from .corpus import Corpus
from .prompt_profiles import BASELINE_PROFILE_ID, get_prompt_profile
from .reasoner import ReplayResearchReasoner
from .tools import SearchProvider, ToolEvent, ToolRegistry, build_research_tools


@dataclass(frozen=True)
class ResearchTrace:
    finish_reason: str
    steps: int
    tool_events: tuple[ToolEvent, ...]
    duration_ms: int
    estimated_tokens: int
    provider_usage: Mapping[str, int] | None
    retrieval_mode: str
    index_version: str | None
    prompt_profile_id: str
    retrieved_candidates: int
    read_chunks: int
    write_calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "finish_reason": self.finish_reason,
            "steps": self.steps,
            "tool_events": [event.__dict__ for event in self.tool_events],
            "duration_ms": self.duration_ms,
            "estimated_tokens": self.estimated_tokens,
            "provider_usage": dict(self.provider_usage) if self.provider_usage is not None else None,
            "retrieval_mode": self.retrieval_mode,
            "index_version": self.index_version,
            "prompt_profile_id": self.prompt_profile_id,
            "retrieved_candidates": self.retrieved_candidates,
            "read_chunks": self.read_chunks,
        }


@dataclass(frozen=True)
class ResearchRun:
    answer: ResearchAnswer
    trace: ResearchTrace

    def to_dict(self) -> dict[str, Any]:
        return {"answer": self.answer.to_dict(), "trace": self.trace.to_dict()}


class ResearchAgent:
    def __init__(
        self,
        corpus: Corpus,
        *,
        reasoner: Reasoner | None = None,
        tools: ToolRegistry | None = None,
        search: SearchProvider | None = None,
        retrieval_mode: str = "lexical",
        index_version: str | None = None,
        prompt_profile_id: str = BASELINE_PROFILE_ID,
        long_term: LongTermMemory | None = None,
        memory_scope: str | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if retrieval_mode not in {"lexical", "hybrid"}:
            raise ValueError("retrieval_mode 必须为 lexical 或 hybrid")
        if tools is not None and search is not None:
            raise ValueError("tools 与 search 不可同时注入")
        if retrieval_mode == "hybrid" and (search is None or not index_version):
            raise ValueError("hybrid retrieval 需要 search 与 index_version")
        if retrieval_mode == "lexical" and index_version is not None:
            raise ValueError("lexical retrieval 不接受 index_version")
        if (long_term is None) != (memory_scope is None):
            raise ValueError("long_term 与 memory_scope 必须成对提供")
        self.corpus = corpus
        self.reasoner = reasoner or ReplayResearchReasoner()
        self.tools = tools or build_research_tools(corpus, search=search)
        self.retrieval_mode = retrieval_mode
        self.index_version = index_version
        self.prompt_profile = get_prompt_profile(prompt_profile_id)
        self.long_term = long_term
        self.memory_scope = memory_scope
        self.clock = clock

    def run(self, question: str) -> ResearchRun:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串")
        self.tools.reset_events()
        short_term = ShortTermMemory(budget_tokens=8192)
        environment = AgentEnvironment(
            tools=self.tools,
            reasoner=self.reasoner,
            strategy="react",
        )
        usage_before = _read_provider_usage(self.reasoner)
        started = self.clock()
        run = environment.run(
            question,
            short_term=short_term,
            long_term=self.long_term,
            query=question,
            memory_scope=self.memory_scope,
            initial_context=(self.prompt_profile.system_context,),
        )
        finished = self.clock()
        usage_after = _read_provider_usage(self.reasoner)
        finish_reason = "completed" if run.trace.success else "step_budget_exhausted"
        if run.trace.success and run.trace.answer is not None:
            try:
                answer = ResearchAnswer.from_json(run.trace.answer)
                self.corpus.validate_answer(answer)
                if answer.status == "insufficient_evidence":
                    finish_reason = answer.reason or "insufficient_evidence"
            except ResearchValidationError:
                answer = ResearchAnswer.refused("invalid_citation_or_output")
                finish_reason = "validation_failed"
        else:
            answer = ResearchAnswer.refused("step_budget_exhausted")
        trace = ResearchTrace(
            finish_reason=finish_reason,
            steps=len(run.trace.records),
            tool_events=self.tools.events(),
            duration_ms=max(0, round((finished - started) * 1000)),
            estimated_tokens=estimate_tokens(question) + estimate_tokens(answer.answer),
            provider_usage=_usage_delta(usage_before, usage_after),
            retrieval_mode=self.retrieval_mode,
            index_version=self.index_version,
            prompt_profile_id=self.prompt_profile.id,
            retrieved_candidates=self.tools.retrieved_candidates(),
            read_chunks=self.tools.read_chunks(),
            write_calls=self.tools.write_calls(),
        )
        return ResearchRun(answer, trace)


def _read_provider_usage(reasoner: Reasoner) -> dict[str, int] | None:
    reader = getattr(reasoner, "token_usage", None)
    if not callable(reader):
        return None
    value = reader()
    if not isinstance(value, Mapping):
        return None
    return {
        "prompt_tokens": max(0, int(value.get("prompt_tokens", 0))),
        "completion_tokens": max(0, int(value.get("completion_tokens", 0))),
    }


def _usage_delta(before: dict[str, int] | None, after: dict[str, int] | None) -> dict[str, int] | None:
    if before is None or after is None:
        return None
    return {key: max(0, after[key] - before[key]) for key in ("prompt_tokens", "completion_tokens")}
