"""Short-term conversation memory: window management and summarization."""

from __future__ import annotations

from dataclasses import dataclass
from .messages import Message, estimate_tokens
from .summarize import DeterministicSummarizer, Summarizer


@dataclass(frozen=True)
class MemoryState:
    summary: str
    recent_messages: tuple[Message, ...]
    budget_tokens: int

    def estimated_tokens(self, chars_per_token: int = 4) -> int:
        """Deterministic heuristic; see messages.estimate_tokens."""
        return estimate_tokens(self.summary, chars_per_token) + sum(
            estimate_tokens(message.content, chars_per_token) for message in self.recent_messages
        )


class ShortTermMemory:
    """Rolling conversation window with greedy folding into a running summary.

    While the estimated total exceeds the budget, the oldest message is folded
    into the summary via the injected summarizer until the estimate returns to
    budget or only the most recent message remains. The summary itself consumes
    budget, so the total estimate can slightly exceed the budget; the visible
    window is never emptied (documented boundary).
    """

    def __init__(
        self,
        *,
        budget_tokens: int = 4096,
        summarizer: Summarizer | None = None,
        chars_per_token: int = 4,
    ) -> None:
        if budget_tokens <= 0:
            raise ValueError("budget_tokens 必须为正整数")
        self.budget_tokens = budget_tokens
        self.chars_per_token = chars_per_token
        self.summarizer: Summarizer = summarizer or DeterministicSummarizer()
        self._summary = ""
        self._messages: list[Message] = []

    def add(self, message: Message) -> MemoryState:
        self._messages.append(message)
        self._compact()
        return self.state()

    def state(self) -> MemoryState:
        return MemoryState(
            summary=self._summary,
            recent_messages=tuple(self._messages),
            budget_tokens=self.budget_tokens,
        )

    def _compact(self) -> None:
        while (
            self.state().estimated_tokens(self.chars_per_token) > self.budget_tokens
            and len(self._messages) > 1
        ):
            oldest = self._messages.pop(0)
            self._summary = self.summarizer.summarize(self._summary, (oldest,))


def inject_memory(memory: MemoryState) -> tuple[Message, ...]:
    """Build the provider payload: optional summary note plus recent messages."""
    injected: list[Message] = []
    if memory.summary:
        injected.append(Message("system", f"【短期记忆摘要】{memory.summary}"))
    return (*injected, *memory.recent_messages)
