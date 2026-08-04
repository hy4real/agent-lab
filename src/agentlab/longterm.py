"""Long-term conversation memory: persisted entries with lexical retrieval."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .memory import MemoryState, inject_memory
from .messages import Message


_MAX_TEXT_LENGTH = 2000
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-z0-9]+")


class LongTermMemoryError(RuntimeError):
    """Stable public error; no paths or raw content in the message."""


def _terms(text: str) -> frozenset[str]:
    lowered = text.lower()
    return frozenset(_LATIN.findall(lowered)) | frozenset(_CJK.findall(lowered))


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    text: str
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise LongTermMemoryError("记忆条目 id 无效")
        if not isinstance(self.text, str) or not self.text.strip():
            raise LongTermMemoryError("记忆条目文本无效")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise LongTermMemoryError("记忆条目时间无效")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryEntry":
        return cls(id=str(value["id"]), text=str(value["text"]), created_at=str(value["created_at"]))


class LongTermMemory:
    """Persisted, deterministic lexical memory.

    Retrieval is a term-overlap baseline (CJK characters plus latin words),
    ranked by overlap count with insertion order as the tie-breaker. It is an
    explicit offline baseline, never a semantic retrieval claim.
    """

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        max_entries: int = 200,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if max_entries <= 0:
            raise LongTermMemoryError("max_entries 必须为正整数")
        self.state_path = Path(state_path) if state_path is not None else Path(".agentlab") / "longterm.json"
        self.max_entries = max_entries
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._entries = self._load()

    def add(self, text: str) -> MemoryEntry:
        if not isinstance(text, str) or not text.strip():
            raise LongTermMemoryError("记忆条目必须是非空字符串")
        if len(text) > _MAX_TEXT_LENGTH:
            raise LongTermMemoryError("记忆条目超过长度上限")
        if any(entry.text == text for entry in self._entries):
            raise LongTermMemoryError("重复记忆条目：相同文本已存在")
        if len(self._entries) >= self.max_entries:
            raise LongTermMemoryError("记忆条目超过数量上限")
        entry = MemoryEntry(
            id=f"entry-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}",
            text=text,
            created_at=self._clock(),
        )
        self._entries.append(entry)
        self.save()
        return entry

    def retrieve(self, query: str, *, top_k: int = 3) -> tuple[MemoryEntry, ...]:
        if top_k <= 0:
            raise LongTermMemoryError("top_k 必须为正整数")
        query_terms = _terms(query)
        ranked = sorted(
            ((len(query_terms & _terms(entry.text)), index, entry) for index, entry in enumerate(self._entries)),
            key=lambda item: (-item[0], item[1]),
        )
        return tuple(entry for score, _index, entry in ranked if score > 0)[:top_k]

    def entries(self) -> tuple[MemoryEntry, ...]:
        return tuple(self._entries)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_path.parent, 0o700)
        fd, temporary = tempfile.mkstemp(dir=self.state_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                payload = json.dumps({"entries": [entry.to_dict() for entry in self._entries]}, ensure_ascii=False, indent=2)
                handle.write(payload.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.close(fd)
            fd = -1
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass

    def _load(self) -> list[MemoryEntry]:
        if not self.state_path.exists():
            return []
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [MemoryEntry.from_dict(item) for item in data["entries"]]
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            raise LongTermMemoryError("长期记忆状态损坏，拒绝继续使用") from exc


def inject_long_term(long_term: LongTermMemory, query: str, *, top_k: int = 3) -> tuple[Message, ...]:
    """Inject retrieved entries as one system block; empty results inject nothing."""
    entries = long_term.retrieve(query, top_k=top_k)
    if not entries:
        return ()
    body = "\n".join(f"- {entry.text}" for entry in entries)
    return (Message("system", f"【不可信历史笔记】仅供规划，不可作为事实或引用来源。\n{body}"),)


def build_full_context(
    short_term: MemoryState,
    long_term: LongTermMemory,
    query: str,
    *,
    top_k: int = 3,
) -> tuple[Message, ...]:
    """Compose the provider payload: long-term block, then short-term context."""
    return (*inject_long_term(long_term, query, top_k=top_k), *inject_memory(short_term))
