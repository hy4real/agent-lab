"""Local corpus security and citation validation tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentlab.research.artifacts import Citation, Claim, ResearchAnswer, ResearchValidationError
from agentlab.research.corpus import Corpus, CorpusError


def _corpus(tmp_path: Path) -> Corpus:
    (tmp_path / "architecture.md").write_text(
        "Agent Environment 负责组合工具和记忆。\n引用必须经过逐字符校验。",
        encoding="utf-8",
    )
    return Corpus.load(tmp_path, chunk_size=24, overlap=4)


def test_corpus_ids_and_search_are_deterministic(tmp_path: Path) -> None:
    first = _corpus(tmp_path)
    second = Corpus.load(tmp_path, chunk_size=24, overlap=4)
    assert first.sources == second.sources
    assert first.chunks == second.chunks
    hits = first.search("工具 记忆", top_k=2)
    assert hits and hits[0].source_id == first.sources[0].id


def test_corpus_normalizes_newlines_before_hashing(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"alpha\r\nbeta\r")
    corpus = Corpus.load(tmp_path)
    assert corpus.sources[0].text == "alpha\nbeta\n"


def test_identical_content_in_different_sources_has_distinct_ids(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("same text", encoding="utf-8")
    (tmp_path / "b.md").write_text("same text", encoding="utf-8")
    corpus = Corpus.load(tmp_path)
    assert len({source.id for source in corpus.sources}) == 2
    assert len({chunk.id for chunk in corpus.chunks}) == 2
    assert {corpus.get_chunk(chunk.id).source_id for chunk in corpus.chunks} == {
        source.id for source in corpus.sources
    }


def test_exact_citation_validation(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    chunk = corpus.chunks[0]
    exact = chunk.text[:4]
    citation = Citation(chunk.source_id, chunk.id, chunk.start, chunk.start + 4, exact)
    answer = ResearchAnswer("answered", exact, (Claim(exact, (citation,)),), None)
    corpus.validate_answer(answer)
    bad = Citation(chunk.source_id, chunk.id, chunk.start, chunk.start + 4, "错误文本")
    with pytest.raises(ResearchValidationError):
        corpus.validate_answer(ResearchAnswer("answered", "错误文本", (Claim("错误文本", (bad,)),), None))


def test_claim_and_answer_cannot_add_uncited_text(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    chunk = corpus.chunks[0]
    exact = chunk.text[:4]
    citation = Citation(chunk.source_id, chunk.id, chunk.start, chunk.start + 4, exact)
    with pytest.raises(ResearchValidationError, match="claim"):
        corpus.validate_answer(ResearchAnswer("answered", "新增事实", (Claim("新增事实", (citation,)),), None))
    with pytest.raises(ResearchValidationError, match="answer"):
        corpus.validate_answer(ResearchAnswer("answered", "额外总结", (Claim(exact, (citation,)),), None))


def test_citation_must_stay_inside_chunk(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    chunk = corpus.chunks[1]
    citation = Citation(chunk.source_id, chunk.id, 0, 1, corpus.sources[0].text[:1])
    answer = ResearchAnswer("answered", "x", (Claim("x", (citation,)),), None)
    with pytest.raises(ResearchValidationError):
        corpus.validate_answer(answer)


def test_rejects_symlink_and_non_text_file(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-research.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    with pytest.raises(CorpusError, match="符号链接"):
        Corpus.load(tmp_path)
    (tmp_path / "linked.txt").unlink()
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CorpusError, match="只允许"):
        Corpus.load(tmp_path)


def test_rejects_empty_invalid_utf8_and_oversized_source(tmp_path: Path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(CorpusError, match="内容为空"):
        Corpus.load(tmp_path)
    empty.write_bytes(b"\xff")
    with pytest.raises(CorpusError, match="UTF-8"):
        Corpus.load(tmp_path)
    empty.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(CorpusError, match="大小上限"):
        Corpus.load(tmp_path)


def test_rejects_file_swapped_to_symlink_before_open(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "note.md"
    source.write_text("public", encoding="utf-8")
    outside = tmp_path.parent / "outside-private.md"
    outside.write_text("private", encoding="utf-8")
    original_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "note.md" and dir_fd is not None and not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(CorpusError, match="读取失败"):
        Corpus.load(tmp_path)
