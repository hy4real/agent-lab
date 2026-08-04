"""Deterministic text chunking with overlap."""

from __future__ import annotations


def chunk_text(text: str, *, chunk_size: int, overlap: int = 0) -> tuple[str, ...]:
    """Fixed-size character chunks; overlap must be smaller than chunk_size."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 必须是非空字符串")
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size")
    step = chunk_size - overlap
    chunks = [text[index : index + chunk_size] for index in range(0, len(text), step)]
    return tuple(chunk for chunk in chunks if chunk)
