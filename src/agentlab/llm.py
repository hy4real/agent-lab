"""Optional real-model backend for Agent Lab evaluation runs.

Only used when the user explicitly authorizes a live evaluation. The adapter
keeps prompts to synthetic tasks and tool observations; no personal data.
"""

from __future__ import annotations

import json
import os
from typing import Mapping, Sequence

from .reasoning import Action


class ProviderError(RuntimeError):
    """Stable Provider boundary error without SDK diagnostics."""


class LLMBackend:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "deepseek-chat",
        temperature: float = 0.0,
    ) -> None:
        try:
            from openai import OpenAI

            self.client = OpenAI(
                api_key=api_key or os.environ.get("OPENAI_API_KEY"),
                base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
            )
        except Exception:
            raise ProviderError("provider_initialization_failed") from None
        self.model = model
        self.temperature = temperature
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=list(messages),  # type: ignore[arg-type]
                temperature=self.temperature,
            )
        except Exception:
            raise ProviderError("provider_request_failed") from None
        usage = response.usage
        if usage is not None:
            self.usage["prompt_tokens"] += usage.prompt_tokens or 0
            self.usage["completion_tokens"] += usage.completion_tokens or 0
        return response.choices[0].message.content or ""


def parse_action(text: str) -> Action:
    """Parse the model's strict JSON action; one repair hint is embedded in prompts."""
    cleaned = text.strip()
    if "```" in cleaned:
        for part in cleaned.split("```"):
            candidate = part.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                cleaned = candidate
                break
    cleaned = cleaned.strip()
    if not cleaned.startswith("{"):
        raise ValueError("模型输出不是 JSON 对象")
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("模型输出不是 JSON 对象")
    if value.get("kind") == "tool":
        return Action(kind="tool", tool=str(value.get("tool") or ""), argument=str(value.get("argument") or ""))
    if value.get("kind") == "finish":
        return Action(kind="finish", answer=str(value.get("answer") or ""))
    raise ValueError("未知 action kind")


def parse_plan(text: str) -> tuple[str, ...]:
    cleaned = text.strip()
    if "```" in cleaned:
        for part in cleaned.split("```"):
            candidate = part.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                cleaned = candidate
                break
    value = json.loads(cleaned)
    if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
        raise ValueError("计划输出必须是 {\"steps\": [...]}")
    steps = tuple(str(item) for item in value["steps"])
    if not steps or not all(step.strip() for step in steps):
        raise ValueError("计划 steps 不能为空")
    return steps


def parse_step(step: str) -> Action:
    """Parse a 'tool(argument)' plan step into a concrete tool action."""
    if "(" not in step or not step.endswith(")"):
        raise ValueError(f"步骤必须是 tool(argument) 格式: {step!r}")
    tool, argument = step.split("(", 1)
    return Action(kind="tool", tool=tool.strip(), argument=argument[:-1].strip())


class ModelReasoner:
    """ReAct reasoner backed by a real model with strict JSON actions."""

    def __init__(self, backend: LLMBackend, *, max_repairs: int = 1) -> None:
        self.backend = backend
        self.max_repairs = max_repairs

    def decide(self, *, task: str, context: tuple[str, ...], available_tools: tuple[str, ...]) -> Action:
        prompt = (
            "你是一个 Agent 推理器。根据任务与观察，输出下一步动作，只输出 JSON，不要解释。\n"
            f"可用工具: {', '.join(available_tools)}\n"
            "输出格式: {\"kind\":\"tool\",\"tool\":\"工具名\",\"argument\":\"参数\"} 或 "
            "{\"kind\":\"finish\",\"answer\":\"最终答案\"}\n"
            f"任务: {task}\n"
            "上下文:\n" + "\n".join(context)
        )
        messages = [{"role": "user", "content": prompt}]
        for _attempt in range(self.max_repairs + 1):
            text = self.backend.complete(messages)
            try:
                return parse_action(text)
            except (ValueError, json.JSONDecodeError) as exc:
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": f"输出必须是合法 JSON，且 kind 为 tool 或 finish。错误: {exc}. 重新输出。"})
        raise RuntimeError("模型连续输出非法 JSON")


class ModelPlanner:
    """Plan-and-Execute planner backed by a real model."""

    def __init__(self, backend: LLMBackend, *, max_repairs: int = 1) -> None:
        self.backend = backend
        self.max_repairs = max_repairs

    def _plan_prompt(self, *, task: str, available_tools: tuple[str, ...], error: str | None = None) -> list[dict[str, str]]:
        instruction = (
            "你是一个任务规划器。为任务输出一个有序步骤列表，每步必须是 tool(argument) 格式。只输出 JSON: {\"steps\": [\"tool(arg)\", ...]}。\n"
            f"可用工具: {', '.join(available_tools)}\n任务: {task}"
        )
        if error:
            instruction += f"\n上一步执行出错: {error}。请重新规划，跳过失败步骤或增加兜底步骤。"
        return [{"role": "user", "content": instruction}]

    def plan(self, *, task: str, available_tools: tuple[str, ...]) -> tuple[str, ...]:
        messages = self._plan_prompt(task=task, available_tools=available_tools)
        for _attempt in range(self.max_repairs + 1):
            text = self.backend.complete(messages)
            try:
                return parse_plan(text)
            except (ValueError, json.JSONDecodeError) as exc:
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": f"输出必须是合法 JSON 且 steps 非空。错误: {exc}. 重新输出。"})
        raise RuntimeError("模型连续输出非法计划")

    def replan(self, *, task: str, plan: tuple[str, ...], context: tuple[str, ...]) -> tuple[str, ...]:
        messages = self._plan_prompt(task=task, available_tools=tuple(), error="\n".join(context[-3:]))
        for _attempt in range(self.max_repairs + 1):
            text = self.backend.complete(messages)
            try:
                return parse_plan(text)
            except (ValueError, json.JSONDecodeError) as exc:
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": f"输出必须是合法 JSON 且 steps 非空。错误: {exc}. 重新输出。"})
        raise RuntimeError("模型连续输出非法计划")
