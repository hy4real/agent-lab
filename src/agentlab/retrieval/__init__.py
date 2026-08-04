"""Lexical retrieval experiment harness: chunking, index, evaluation."""

from .chunking import chunk_text
from .evaluate import evaluate, hit_at_k, recall_at_k
from .experiment import ChunkingReport, run_chunking_experiment, render_experiment
from .index import LexicalIndex

__all__ = [
    "ChunkingReport",
    "LexicalIndex",
    "chunk_text",
    "evaluate",
    "hit_at_k",
    "recall_at_k",
    "render_experiment",
    "run_chunking_experiment",
]
