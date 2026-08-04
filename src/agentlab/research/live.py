"""Explicitly authorized live-model reasoner for Research Agent."""

from __future__ import annotations

import json
from typing import Mapping, Protocol, Sequence

from ..llm import parse_action
from ..reasoning import Action
from .prompt_profiles import BASELINE_PROFILE_ID, get_prompt_profile


class CompletionBackend(Protocol):
    usage: dict[str, int]

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        ...


class LiveResearchReasoner:
    def __init__(
        self,
        backend: CompletionBackend,
        *,
        max_repairs: int = 1,
        prompt_profile_id: str = BASELINE_PROFILE_ID,
    ) -> None:
        self.backend = backend
        self.max_repairs = max_repairs
        self.prompt_profile = get_prompt_profile(prompt_profile_id)

    def token_usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": max(0, int(self.backend.usage.get("prompt_tokens", 0))),
            "completion_tokens": max(0, int(self.backend.usage.get("completion_tokens", 0))),
        }

    def decide(self, *, task: str, context: tuple[str, ...], available_tools: tuple[str, ...]) -> Action:
        prompt = (
            "You are an evidence research controller. "
            f"{self.prompt_profile.controller_instructions} Return one JSON action and no prose.\n"
            f"Tools: {', '.join(available_tools)}\n"
            "Tool action schema: {\"kind\":\"tool\",\"tool\":\"name\",\"argument\":\"JSON object string\"}.\n"
            "Finish action schema: {\"kind\":\"finish\",\"answer\":\"ResearchAnswer JSON string\"}.\n"
            "ResearchAnswer must have exactly status, answer, claims, reason. Each claim must have text and citations; "
            "each citation must have source_id, chunk_id, start, end, exact_text copied exactly from read_chunk. "
            "If evidence is insufficient, return status insufficient_evidence, empty answer and claims, and a reason.\n"
            f"Question: {task}\nContext:\n" + "\n".join(context)
        )
        messages: list[Mapping[str, str]] = [{"role": "user", "content": prompt}]
        for _attempt in range(self.max_repairs + 1):
            output = self.backend.complete(messages)
            try:
                return parse_action(output)
            except (ValueError, json.JSONDecodeError):
                messages.extend((
                    {"role": "assistant", "content": output},
                    {"role": "user", "content": "Return one valid JSON action matching the specified schema."},
                ))
        raise RuntimeError("live_model_invalid_action")
