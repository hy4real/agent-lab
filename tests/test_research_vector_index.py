"""Optional Qdrant local index contract tests; no model or network required."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("qdrant_client")

import agentlab.research.vector_index as vector_index
from agentlab.research.corpus import Corpus
from agentlab.research.agent import ResearchAgent
from agentlab.research.retrieval import HybridRetriever
from agentlab.research.vector_index import (
    EMBEDDING_MODEL,
    FastEmbedProvider,
    IndexManifest,
    MANIFEST_NAME,
    QdrantVectorSearch,
    RagIndexError,
    build_local_index,
    verify_local_index,
)


class SyntheticEmbeddings:
    model_name = EMBEDDING_MODEL
    backend_name = "synthetic"
    backend_version = "1.0"

    def embed(self, texts):
        return tuple(
            (
                1.0 if "automobile" in text.lower() or "car" in text.lower() else 0.0,
                1.0 if "weather" in text.lower() else 0.0,
                0.5,
            )
            for text in texts
        )


def _corpus(root: Path, *, transport: str = "An automobile needs regular service.") -> Corpus:
    root.mkdir()
    (root / "transport.md").write_text(transport, encoding="utf-8")
    (root / "weather.md").write_text("Weather is sunny.", encoding="utf-8")
    return Corpus.load(root, chunk_size=80, overlap=10)


def _clock() -> datetime:
    return datetime(2026, 8, 2, 0, 0, tzinfo=UTC)


def test_build_verify_and_query_persistent_local_index(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / "corpus")
    index_dir = tmp_path / "index"
    manifest = build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)
    assert manifest.corpus_hash == corpus.content_hash
    assert manifest.chunk_hashes == corpus.chunk_hashes
    assert manifest.dimension == 3
    assert manifest.embedding_backend == "synthetic"
    assert manifest.embedding_backend_version == "1.0"
    assert manifest.point_count == len(corpus.chunks)
    assert verify_local_index(corpus, index_dir, SyntheticEmbeddings()) == manifest

    search = QdrantVectorSearch(corpus, index_dir, SyntheticEmbeddings())
    try:
        hits = search.search("car maintenance", top_k=2)
        assert hits[0].source_name == "transport.md"
        assert search.index_version == manifest.index_version
    finally:
        search.close()


def test_qdrant_hybrid_search_runs_through_research_agent_mainline(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / "corpus")
    index_dir = tmp_path / "index"
    manifest = build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)
    vector = QdrantVectorSearch(corpus, index_dir, SyntheticEmbeddings())
    try:
        run = ResearchAgent(
            corpus,
            search=HybridRetriever(corpus, vector),
            retrieval_mode="hybrid",
            index_version=manifest.index_version,
            prompt_profile_id="grounded-hybrid-v1",
            clock=lambda: 0.0,
        ).run("How should a car receive regular service?")
    finally:
        vector.close()
    assert run.trace.retrieval_mode == "hybrid"
    assert run.trace.index_version == manifest.index_version
    assert run.trace.retrieved_candidates > 0
    assert any(event.name == "search_corpus" for event in run.trace.tool_events)
    assert any(event.name == "read_chunk" for event in run.trace.tool_events)


def test_manifest_is_strict_sanitized_and_identity_is_stable(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / "corpus")
    index_dir = tmp_path / "index"
    first = build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)
    raw = (index_dir / MANIFEST_NAME).read_text(encoding="utf-8")
    assert str(tmp_path) not in raw
    assert "automobile" not in raw.lower()
    value = json.loads(raw)
    value["unknown"] = True
    (index_dir / MANIFEST_NAME).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RagIndexError, match="index_manifest_invalid"):
        verify_local_index(corpus, index_dir, SyntheticEmbeddings())

    value.pop("unknown")
    (index_dir / MANIFEST_NAME).write_text(json.dumps(value), encoding="utf-8")
    second = build_local_index(
        corpus,
        index_dir,
        SyntheticEmbeddings(),
        rebuild=True,
        clock=_clock,
    )
    assert second.index_version == first.index_version


def test_corpus_and_chunking_changes_make_index_stale(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    corpus = _corpus(corpus_root)
    index_dir = tmp_path / "index"
    build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)

    (corpus_root / "transport.md").write_text("An automobile changed.", encoding="utf-8")
    with pytest.raises(RagIndexError, match="index_stale"):
        verify_local_index(
            Corpus.load(corpus_root, chunk_size=80, overlap=10),
            index_dir,
            SyntheticEmbeddings(),
        )
    with pytest.raises(RagIndexError, match="index_stale"):
        verify_local_index(
            Corpus.load(corpus_root, chunk_size=60, overlap=5),
            index_dir,
            SyntheticEmbeddings(),
        )


def test_embedding_behavior_change_makes_index_stale(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / "corpus")
    index_dir = tmp_path / "index"
    build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)

    class ChangedEmbeddings(SyntheticEmbeddings):
        def embed(self, texts):
            return tuple((*vector[:2], 0.75) for vector in super().embed(texts))

    with pytest.raises(RagIndexError, match="index_stale"):
        verify_local_index(corpus, index_dir, ChangedEmbeddings())


def test_point_payload_and_count_are_verified(tmp_path: Path) -> None:
    from qdrant_client import QdrantClient

    corpus = _corpus(tmp_path / "corpus")
    index_dir = tmp_path / "index"
    build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)
    client = QdrantClient(
        path=str(index_dir / "qdrant"),
        force_disable_check_same_thread=True,
    )
    try:
        client.delete("agentlab_chunks_v1", points_selector=[0], wait=True)
    finally:
        client.close()
    with pytest.raises(RagIndexError, match="index_point_count_mismatch"):
        verify_local_index(corpus, index_dir, SyntheticEmbeddings())


def test_existing_index_requires_explicit_rebuild(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / "corpus")
    index_dir = tmp_path / "index"
    build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)
    with pytest.raises(RagIndexError, match="index_exists"):
        build_local_index(corpus, index_dir, SyntheticEmbeddings(), clock=_clock)


def test_fastembed_provider_validates_optional_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None, bool]] = []

    class FakeTextEmbedding:
        def __init__(self, *, model_name, cache_dir, lazy_load):
            calls.append((model_name, cache_dir, lazy_load))

        def embed(self, texts):
            return ([float(index), 0.5] for index, _text in enumerate(texts, start=1))

    monkeypatch.setattr(
        vector_index.importlib,
        "import_module",
        lambda name: SimpleNamespace(TextEmbedding=FakeTextEmbedding),
    )
    monkeypatch.setattr(vector_index, "version", lambda name: "0.8.0")
    provider = FastEmbedProvider(cache_dir=tmp_path)

    assert calls == [(EMBEDDING_MODEL, str(tmp_path), True)]
    assert provider.backend_version == "0.8.0"
    assert provider.embed(("one", "two")) == ((1.0, 0.5), (2.0, 0.5))
    with pytest.raises(RagIndexError, match="embedding_input_invalid"):
        provider.embed(())


def test_fastembed_provider_maps_dependency_and_embedding_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_module(_name: str):
        raise ImportError("private diagnostic")

    monkeypatch.setattr(vector_index.importlib, "import_module", missing_module)
    with pytest.raises(RagIndexError, match="rag_extra_missing"):
        FastEmbedProvider()

    class BrokenTextEmbedding:
        def __init__(self, **_kwargs):
            pass

        def embed(self, _texts):
            raise RuntimeError("private diagnostic")

    monkeypatch.setattr(
        vector_index.importlib,
        "import_module",
        lambda name: SimpleNamespace(TextEmbedding=BrokenTextEmbedding),
    )
    monkeypatch.setattr(vector_index, "version", lambda name: "0.8.0")
    with pytest.raises(RagIndexError, match="embedding_failed"):
        FastEmbedProvider().embed(("synthetic",))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"schema_version": "unknown"}),
        lambda value: value["chunking"].update({"unknown": 1}),
        lambda value: value.update({"chunk_hashes": []}),
        lambda value: value.update({"created_at": "2026-08-02T00:00:00"}),
        lambda value: value["chunking"].update({"overlap": value["chunking"]["chunk_size"]}),
    ],
)
def test_manifest_rejects_malformed_contract(tmp_path: Path, mutation) -> None:
    corpus = _corpus(tmp_path / "corpus")
    manifest = IndexManifest.create(
        corpus,
        embedding_backend="synthetic",
        embedding_backend_version="1.0",
        embedding_model=EMBEDDING_MODEL,
        embedding_probe_hash="a" * 64,
        dimension=3,
        created_at=_clock().isoformat(),
    )
    value = manifest.to_dict()
    mutation(value)
    with pytest.raises(RagIndexError, match="index_manifest_invalid"):
        IndexManifest.from_dict(value)


def test_qdrant_local_uses_explicit_thread_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    client = object()
    models = object()

    def client_factory(**kwargs):
        calls.append(kwargs)
        return client

    monkeypatch.setattr(
        vector_index.importlib,
        "import_module",
        lambda name: SimpleNamespace(QdrantClient=client_factory, models=models),
    )

    assert vector_index._open_qdrant(tmp_path) == (client, models)
    assert calls == [{
        "path": str(tmp_path),
        "force_disable_check_same_thread": True,
    }]
