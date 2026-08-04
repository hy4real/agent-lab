"""Bounded local corpus with deterministic source and chunk identities."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifacts import ResearchAnswer, ResearchValidationError


MAX_SOURCES = 32
MAX_SOURCE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 1200
DEFAULT_OVERLAP = 200
_ALLOWED_SUFFIXES = frozenset({".md", ".txt"})
_OPEN_BASE = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_OPEN_DIRECTORY = _OPEN_BASE | getattr(os, "O_DIRECTORY", 0)
_OPEN_FILE = _OPEN_BASE | getattr(os, "O_NONBLOCK", 0)
_SLUG = re.compile(r"[^a-z0-9]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN = re.compile(r"[a-z0-9]+")
_LATIN_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "the", "to", "what", "when", "where",
    "which", "with",
})


class CorpusError(ValueError):
    """Stable corpus error; messages never include local paths or content."""


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def research_terms(text: str) -> frozenset[str]:
    """Latin words plus CJK bigrams avoid single-character false positives."""
    lowered = text.lower()
    terms = set(_LATIN.findall(lowered)) - _LATIN_STOPWORDS
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return frozenset(terms)


def _identifier(prefix: str, name: str, digest: str) -> str:
    slug = _SLUG.sub("-", name.lower()).strip("-")[:32]
    middle = f"-{slug}" if slug else ""
    return f"{prefix}{middle}-{digest[:12]}"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    content_hash: str
    text: str


@dataclass(frozen=True)
class Chunk:
    id: str
    source_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    source_id: str
    source_name: str
    score: float


class Corpus:
    def __init__(
        self,
        sources: Sequence[Source],
        chunks: Sequence[Chunk],
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_OVERLAP,
    ) -> None:
        if not sources or not chunks:
            raise CorpusError("corpus 必须包含非空文本来源")
        if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
            raise CorpusError("chunk 参数无效")
        self.sources = tuple(sources)
        self.chunks = tuple(chunks)
        self.chunk_size = chunk_size
        self.overlap = overlap
        self._sources = {source.id: source for source in self.sources}
        self._chunks = {chunk.id: chunk for chunk in self.chunks}
        self.chunk_hashes = tuple(
            _digest(f"{chunk.source_id}\n{chunk.start}\n{chunk.end}\n{chunk.text}")
            for chunk in self.chunks
        )
        corpus_identity = {
            "chunk_hashes": self.chunk_hashes,
            "chunk_size": self.chunk_size,
            "overlap": self.overlap,
            "sources": tuple((source.id, source.content_hash) for source in self.sources),
        }
        self.content_hash = hashlib.sha256(
            json.dumps(
                corpus_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def load(
        cls,
        root: Path,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_OVERLAP,
    ) -> "Corpus":
        root = Path(root)
        if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
            raise CorpusError("chunk 参数无效")

        files = _read_corpus_files(root)
        if not files:
            raise CorpusError("corpus 没有文本来源")

        sources: list[Source] = []
        chunks: list[Chunk] = []
        for relative_name, raw in files:
            try:
                text = _normalize(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise CorpusError("corpus 来源不是合法 UTF-8") from exc
            if not text.strip():
                raise CorpusError("corpus 来源内容为空")
            content_hash = _digest(text)
            identity_hash = _digest(f"{relative_name}\n{content_hash}")
            source_id = _identifier("source", Path(relative_name).stem, identity_hash)
            source = Source(source_id, relative_name, content_hash, text)
            sources.append(source)
            chunks.extend(_chunks_for(source, chunk_size=chunk_size, overlap=overlap))
        return cls(sources, chunks, chunk_size=chunk_size, overlap=overlap)

    def list_sources(self) -> tuple[Source, ...]:
        return self.sources

    def search(self, query: str, *, top_k: int = 5) -> tuple[SearchHit, ...]:
        if not isinstance(query, str) or not query.strip():
            raise CorpusError("search query 必须是非空字符串")
        if top_k <= 0 or top_k > 10:
            raise CorpusError("search top_k 必须在 1..10")
        query_terms = research_terms(query)
        scored: list[tuple[int, int, Chunk]] = []
        for index, chunk in enumerate(self.chunks):
            score = len(query_terms & research_terms(chunk.text))
            if score:
                scored.append((score, index, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            SearchHit(chunk.id, chunk.source_id, self._sources[chunk.source_id].name, float(score))
            for score, _index, chunk in scored[:top_k]
        )

    def get_chunk(self, chunk_id: str) -> Chunk:
        try:
            return self._chunks[chunk_id]
        except KeyError as exc:
            raise CorpusError("未知 chunk_id") from exc

    def validate_answer(self, answer: ResearchAnswer) -> None:
        for claim in answer.claims:
            if not any(claim.text == citation.exact_text for citation in claim.citations):
                raise ResearchValidationError("claim 文本未被逐字引用支持")
            for citation in claim.citations:
                source = self._sources.get(citation.source_id)
                chunk = self._chunks.get(citation.chunk_id)
                if source is None or chunk is None or chunk.source_id != citation.source_id:
                    raise ResearchValidationError("citation 引用未知来源")
                if citation.start < chunk.start or citation.end > chunk.end:
                    raise ResearchValidationError("citation 超出 chunk 范围")
                if source.text[citation.start : citation.end] != citation.exact_text:
                    raise ResearchValidationError("citation exact_text 不匹配")
        if answer.status == "answered" and answer.answer != "\n".join(claim.text for claim in answer.claims):
            raise ResearchValidationError("answer 包含 citation 之外的文本")


def _read_corpus_files(root: Path) -> list[tuple[str, bytes]]:
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise CorpusError("当前平台不支持安全 corpus 读取")
    try:
        root_fd = os.open(os.fspath(root), _OPEN_DIRECTORY)
    except OSError as exc:
        raise CorpusError("corpus root 必须是非符号链接目录") from exc
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise CorpusError("corpus root 必须是非符号链接目录")
        files: list[tuple[str, bytes]] = []
        _walk_corpus(root_fd, (), files)
        return sorted(files, key=lambda item: item[0])
    except CorpusError:
        raise
    except OSError as exc:
        raise CorpusError("corpus 目录读取失败") from exc
    finally:
        os.close(root_fd)


def _walk_corpus(dir_fd: int, parents: tuple[str, ...], files: list[tuple[str, bytes]]) -> None:
    for name in sorted(os.listdir(dir_fd)):
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise CorpusError("corpus 不允许符号链接")
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _OPEN_DIRECTORY, dir_fd=dir_fd)
            try:
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    raise CorpusError("corpus 来源必须是普通文件")
                _walk_corpus(child_fd, (*parents, name), files)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise CorpusError("corpus 来源必须是普通文件")
        if Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
            raise CorpusError("corpus 只允许 .md/.txt 文件")
        if len(files) >= MAX_SOURCES:
            raise CorpusError("corpus 来源数量超过上限")
        relative_name = "/".join((*parents, name))
        try:
            relative_name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CorpusError("corpus 来源名称不是合法 UTF-8") from exc
        remaining = MAX_TOTAL_BYTES - sum(len(content) for _path, content in files)
        files.append((relative_name, _read_regular_file(dir_fd, name, remaining)))


def _read_regular_file(dir_fd: int, name: str, remaining: int) -> bytes:
    try:
        file_fd = os.open(name, _OPEN_FILE, dir_fd=dir_fd)
    except OSError as exc:
        raise CorpusError("corpus 来源读取失败") from exc
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise CorpusError("corpus 来源必须是普通文件")
        if info.st_size > MAX_SOURCE_BYTES:
            raise CorpusError("corpus 单来源超过大小上限")
        if info.st_size > remaining:
            raise CorpusError("corpus 总大小超过上限")
        limit = min(MAX_SOURCE_BYTES, remaining)
        chunks = bytearray()
        while len(chunks) <= limit:
            block = os.read(file_fd, min(64 * 1024, limit + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > MAX_SOURCE_BYTES:
            raise CorpusError("corpus 单来源超过大小上限")
        if len(chunks) > remaining:
            raise CorpusError("corpus 总大小超过上限")
        return bytes(chunks)
    except OSError as exc:
        raise CorpusError("corpus 来源读取失败") from exc
    finally:
        os.close(file_fd)


def _chunks_for(source: Source, *, chunk_size: int, overlap: int) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    step = chunk_size - overlap
    for start in range(0, len(source.text), step):
        end = min(len(source.text), start + chunk_size)
        text = source.text[start:end]
        digest = _digest(f"{source.id}\n{start}\n{end}\n{text}")
        chunks.append(Chunk(_identifier("chunk", "", digest), source.id, start, end, text))
        if end == len(source.text):
            break
    return tuple(chunks)
