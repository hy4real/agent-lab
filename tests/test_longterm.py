"""Long-term memory contract tests (fully offline, deterministic)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentlab.demo import run_longterm_demo
from agentlab.longterm import LongTermMemory, LongTermMemoryError, build_full_context, inject_long_term
from agentlab.memory import MemoryState
from agentlab.messages import Message


def _clock() -> str:
    return "2026-07-31T00:00:00+00:00"


def test_add_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "state" / "longterm.json"
    memory = LongTermMemory(state_path=path, clock=_clock)
    entry = memory.add("项目 Alpha 使用异步队列处理任务")
    assert entry.id.startswith("entry-")
    reloaded = LongTermMemory(state_path=path, clock=_clock)
    assert reloaded.entries() == (entry,)


def test_state_file_permissions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "longterm.json"
    LongTermMemory(state_path=path, clock=_clock).add("x")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_retrieval_is_deterministic_and_respects_top_k(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    memory.add("项目 Beta 实现 LRU 缓存并压测命中率")
    memory.add("项目 Alpha 使用异步队列处理任务")
    memory.add("会议结论：优先可离线复现的评测")
    first = memory.retrieve("缓存 离线 评测", top_k=2)
    second = memory.retrieve("缓存 离线 评测", top_k=2)
    assert first == second
    assert len(first) == 2


def test_retrieval_scores_higher_overlap_first(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    memory.add("只含一个无关词")
    memory.add("缓存与离线评测完全匹配")
    result = memory.retrieve("缓存 离线 评测", top_k=3)
    assert result[0].text == "缓存与离线评测完全匹配"


def test_cjk_and_latin_terms(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    memory.add("使用 Python 构建 agent 工具链")
    memory.add("使用 Go 构建服务")
    result = memory.retrieve("python agent", top_k=2)
    assert result[0].text == "使用 Python 构建 agent 工具链"


def test_empty_memory_returns_nothing(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    assert memory.retrieve("anything", top_k=3) == ()
    assert inject_long_term(memory, "anything") == ()


def test_duplicate_text_rejected(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    memory.add("重复文本")
    with pytest.raises(LongTermMemoryError):
        memory.add("重复文本")


def test_max_entries_enforced(tmp_path: Path) -> None:
    memory = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock, max_entries=1)
    memory.add("one")
    with pytest.raises(LongTermMemoryError):
        memory.add("two")


def test_corrupt_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "longterm.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(LongTermMemoryError):
        LongTermMemory(state_path=path, clock=_clock)


def test_full_context_order(tmp_path: Path) -> None:
    long_term = LongTermMemory(state_path=tmp_path / "longterm.json", clock=_clock)
    long_term.add("缓存方案已定")
    short_term = MemoryState(summary="短期摘要", recent_messages=(Message("user", "最近消息"),), budget_tokens=100)
    context = build_full_context(short_term, long_term, "缓存", top_k=1)
    assert context[0].role == "system" and "不可信历史笔记" in context[0].content
    assert "不可作为事实或引用来源" in context[0].content
    assert context[1].role == "system" and "短期记忆摘要" in context[1].content
    assert context[2:] == short_term.recent_messages


def test_longterm_demo_is_reproducible() -> None:
    assert run_longterm_demo() == run_longterm_demo()
    assert "retrieved:" in run_longterm_demo()
