"""Summarizer protocol and a deterministic reference implementation."""

from __future__ import annotations

from typing import Protocol, Sequence

from .messages import Message


MAX_SUMMARY_LENGTH = 600
_HEAD_LIMIT = 80


class Summarizer(Protocol):
    """Fold older messages into a running summary."""

    def summarize(self, existing_summary: str, messages: Sequence[Message]) -> str:
        ...


class DeterministicSummarizer:
    """Offline, reproducible summarizer used by tests and the demo.

    Production deployments may swap in a model-backed implementation with the
    same protocol; memory behavior must not depend on which summarizer runs.
    """

    def summarize(self, existing_summary: str, messages: Sequence[Message]) -> str:
        parts = [existing_summary] if existing_summary else []
        for message in messages:
            head = " ".join(message.content.split())[:_HEAD_LIMIT]
            parts.append(f"[{message.role}] {head}")
        digest = "；".join(part for part in parts if part)
        if len(digest) > MAX_SUMMARY_LENGTH:
            digest = digest[:MAX_SUMMARY_LENGTH] + "…"
        return digest
