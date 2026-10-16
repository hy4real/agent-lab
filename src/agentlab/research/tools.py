"""Callable research tools with strict JSON arguments and stable errors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from ..reasoning import ToolError
from .corpus import Corpus, CorpusError


class SearchProvider(Protocol):
    def search(self, query: str, *, top_k: int = 5):
        ...


@dataclass(frozen=True)
class ToolEvent:
    name: str
    status: str


SIDE_EFFECTS = frozenset({"none", "read", "write"})


@dataclass(frozen=True)
class ToolSpec:
    """Declared contract of one callable tool.

    ``side_effect`` and ``max_output_bytes`` are mandatory on purpose: AGENTS.md
    requires every tool to state what it may touch and how large its serialised
    result may get. A tool that keeps those implicit cannot be audited.
    """

    required: Mapping[str, type]
    optional: Mapping[str, type]
    handler: Callable[[Mapping[str, Any]], Any]
    side_effect: str
    max_output_bytes: int

    def __post_init__(self) -> None:
        if self.side_effect not in SIDE_EFFECTS:
            raise ValueError("side_effect 必须是 none / read / write")
        if not isinstance(self.max_output_bytes, int) or self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes 必须是正整数")


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._events: list[ToolEvent] = []
        self._retrieved_candidates = 0
        self._read_chunks = 0
        self._write_calls = 0

    def register(self, name: str, spec: ToolSpec) -> None:
        if not name or name in self._specs:
            raise ValueError("工具名称为空或重复")
        self._specs[name] = spec

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def events(self) -> tuple[ToolEvent, ...]:
        return tuple(self._events)

    def reset_events(self) -> None:
        self._events.clear()
        self._retrieved_candidates = 0
        self._read_chunks = 0
        self._write_calls = 0

    def retrieved_candidates(self) -> int:
        return self._retrieved_candidates

    def read_chunks(self) -> int:
        return self._read_chunks

    def write_calls(self) -> int:
        """Count of successful calls to tools declared as ``write``."""
        return self._write_calls

    def call(self, tool: str, argument: str) -> str:
        spec = self._specs.get(tool)
        if spec is None:
            self._events.append(ToolEvent(tool if tool in self._specs else "unknown", "error"))
            raise ToolError("unknown_tool")
        try:
            value = json.loads(argument)
            if not isinstance(value, dict):
                raise ToolError("invalid_arguments")
            self._validate(value, spec)
            result = spec.handler(value)
        except json.JSONDecodeError as exc:
            self._events.append(ToolEvent(tool, "error"))
            raise ToolError("invalid_arguments") from exc
        except (CorpusError, TypeError, ValueError) as exc:
            self._events.append(ToolEvent(tool, "error"))
            raise ToolError("tool_failure") from exc
        except ToolError:
            self._events.append(ToolEvent(tool, "error"))
            raise
        except Exception as exc:
            self._events.append(ToolEvent(tool, "error"))
            raise ToolError("tool_failure") from exc
        payload = json.dumps(
            {"ok": True, "tool": tool, "data": result},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > spec.max_output_bytes:
            self._events.append(ToolEvent(tool, "error"))
            raise ToolError("output_too_large")
        if tool == "search_corpus" and isinstance(result, list):
            self._retrieved_candidates += len(result)
        elif tool == "read_chunk":
            self._read_chunks += 1
        if spec.side_effect == "write":
            self._write_calls += 1
        self._events.append(ToolEvent(tool, "ok"))
        return payload

    @staticmethod
    def _validate(value: Mapping[str, Any], spec: ToolSpec) -> None:
        expected = set(spec.required) | set(spec.optional)
        if set(value) - expected or set(spec.required) - set(value):
            raise ToolError("invalid_arguments")
        for key, expected_type in (*spec.required.items(), *spec.optional.items()):
            if key not in value:
                continue
            item = value[key]
            if isinstance(item, bool) and expected_type is int:
                raise ToolError("invalid_arguments")
            if not isinstance(item, expected_type):
                raise ToolError("invalid_arguments")


def build_research_tools(corpus: Corpus, *, search: SearchProvider | None = None) -> ToolRegistry:
    search_provider = search or corpus
    registry = ToolRegistry()
    registry.register(
        "list_sources",
        ToolSpec(
            required={},
            optional={},
            side_effect="read",
            max_output_bytes=32 * 1024,
            handler=lambda _args: [
                {"source_id": source.id, "name": source.name, "content_hash": source.content_hash}
                for source in corpus.list_sources()
            ],
        ),
    )
    registry.register(
        "search_corpus",
        ToolSpec(
            required={"query": str},
            optional={"top_k": int},
            side_effect="read",
            max_output_bytes=16 * 1024,
            handler=lambda args: [
                {
                    "chunk_id": hit.chunk_id,
                    "source_id": hit.source_id,
                    "source_name": hit.source_name,
                    "score": hit.score,
                }
                for hit in search_provider.search(str(args["query"]), top_k=int(args.get("top_k", 5)))
            ],
        ),
    )
    registry.register(
        "read_chunk",
        ToolSpec(
            required={"chunk_id": str},
            optional={},
            side_effect="read",
            max_output_bytes=8 * 1024,
            handler=lambda args: _render_chunk(corpus, str(args["chunk_id"])),
        ),
    )
    return registry


def _render_chunk(corpus: Corpus, chunk_id: str) -> dict[str, Any]:
    chunk = corpus.get_chunk(chunk_id)
    return {
        "chunk_id": chunk.id,
        "source_id": chunk.source_id,
        "start": chunk.start,
        "end": chunk.end,
        "text": chunk.text,
    }
