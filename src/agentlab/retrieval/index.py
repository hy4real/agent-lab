"""Deterministic lexical index over chunks (CJK chars + latin words)."""

from __future__ import annotations

import re
from typing import Sequence


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-z0-9]+")


def terms_of(text: str) -> frozenset[str]:
    lowered = text.lower()
    return frozenset(_LATIN.findall(lowered)) | frozenset(_CJK.findall(lowered))


class LexicalIndex:
    def __init__(self, chunks: Sequence[str]) -> None:
        if not chunks:
            raise ValueError("至少需要一个 chunk")
        self.chunks = tuple(chunks)
        self._postings: dict[str, set[int]] = {}
        for index, chunk in enumerate(self.chunks):
            for term in terms_of(chunk):
                self._postings.setdefault(term, set()).add(index)

    def search(self, query: str, *, top_k: int = 3) -> tuple[int, ...]:
        if top_k <= 0:
            raise ValueError("top_k 必须为正整数")
        query_terms = terms_of(query)
        scored = []
        for index in range(len(self.chunks)):
            chunk_terms = terms_of(self.chunks[index])
            score = len(query_terms & chunk_terms)
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(index for _score, index in scored[:top_k])
