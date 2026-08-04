"""Deterministic replay reasoner for the offline Research Agent."""

from __future__ import annotations

import json
import re
from typing import Any

from ..reasoning import Action
from .artifacts import Citation, Claim, ResearchAnswer
from .corpus import research_terms


_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")


class ReplayResearchReasoner:
    """Exercise the real tool loop without a model or network."""

    def __init__(self, *, max_read_chunks: int = 5) -> None:
        if max_read_chunks <= 0 or max_read_chunks > 5:
            raise ValueError("max_read_chunks 必须在 1..5")
        self.max_read_chunks = max_read_chunks

    def decide(self, *, task: str, context: tuple[str, ...], available_tools: tuple[str, ...]) -> Action:
        observations, has_error = _observations(context)
        by_tool: dict[str, list[Any]] = {}
        for observation in observations:
            by_tool.setdefault(str(observation["tool"]), []).append(observation["data"])
        if has_error:
            return Action(kind="finish", answer=ResearchAnswer.refused("tool_failure").to_json())
        if "list_sources" not in by_tool:
            return Action(kind="tool", tool="list_sources", argument="{}")
        if "search_corpus" not in by_tool:
            argument = json.dumps({"query": task, "top_k": self.max_read_chunks}, ensure_ascii=False)
            return Action(kind="tool", tool="search_corpus", argument=argument)

        hits = by_tool["search_corpus"][-1]
        if not isinstance(hits, list) or not hits:
            return Action(kind="finish", answer=ResearchAnswer.refused("no_relevant_source").to_json())
        max_score = max(float(hit["score"]) for hit in hits)
        target_ids = [
            str(hit["chunk_id"])
            for hit in hits[: self.max_read_chunks]
            if float(hit["score"]) >= max_score * 0.5
        ]
        reads = [item for item in by_tool.get("read_chunk", []) if isinstance(item, dict)]
        read_ids = {str(item.get("chunk_id")) for item in reads}
        for chunk_id in target_ids:
            if chunk_id not in read_ids:
                argument = json.dumps({"chunk_id": chunk_id}, ensure_ascii=False)
                return Action(kind="tool", tool="read_chunk", argument=argument)

        answer = _answer_from_reads(task, reads)
        return Action(kind="finish", answer=answer.to_json())


def _observations(context: tuple[str, ...]) -> tuple[list[dict[str, Any]], bool]:
    observations: list[dict[str, Any]] = []
    seen: set[str] = set()
    has_error = False
    for item in context:
        if item.startswith("观察: "):
            payload = item[len("观察: ") :]
        elif item.startswith("tool: 观察: "):
            payload = item[len("tool: 观察: ") :]
        else:
            continue
        if payload.startswith("error:"):
            has_error = True
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("ok") is not True or "tool" not in value or "data" not in value:
            continue
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if canonical not in seen:
            observations.append(value)
            seen.add(canonical)
    return observations, has_error


def _answer_from_reads(question: str, reads: list[dict[str, Any]]) -> ResearchAnswer:
    claims: list[Claim] = []
    seen_sources: set[str] = set()
    for read in reads:
        source_id = str(read.get("source_id", ""))
        chunk_id = str(read.get("chunk_id", ""))
        text = str(read.get("text", ""))
        chunk_start = read.get("start")
        if not source_id or source_id in seen_sources or not isinstance(chunk_start, int):
            continue
        selected = _best_sentence(question, text)
        if selected is None:
            continue
        local_start, exact_text = selected
        start = chunk_start + local_start
        citation = Citation(source_id, chunk_id, start, start + len(exact_text), exact_text)
        claims.append(Claim(exact_text, (citation,)))
        seen_sources.add(source_id)
    if not claims:
        return ResearchAnswer.refused("no_citable_evidence")
    return ResearchAnswer(
        status="answered",
        answer="\n".join(claim.text for claim in claims),
        claims=tuple(claims),
        reason=None,
    )


def _best_sentence(question: str, text: str) -> tuple[int, str] | None:
    query_terms = research_terms(question)
    ranked: list[tuple[int, int, str]] = []
    for match in _SENTENCE.finditer(text):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        sentence = raw.strip()
        if not sentence:
            continue
        score = len(query_terms & research_terms(sentence))
        if score:
            ranked.append((score, match.start() + leading, sentence))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1]))
    _score, start, sentence = ranked[0]
    return start, sentence
