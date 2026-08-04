"""Optional FastEmbed + Qdrant local vector index with strict provenance."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .corpus import Corpus, CorpusError, SearchHit


EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SCHEMA_VERSION = "agentlab-rag-index-v1"
COLLECTION_NAME = "agentlab_chunks_v1"
DISTANCE = "cosine"
MANIFEST_NAME = "index-manifest.json"
EMBEDDING_PROBE = "agentlab-index-probe-v1"
_MANIFEST_KEYS = frozenset({
    "schema_version",
    "index_version",
    "corpus_hash",
    "chunk_hashes",
    "chunking",
    "embedding_backend",
    "embedding_model",
    "embedding_probe_hash",
    "dimension",
    "distance",
    "point_count",
    "created_at",
})


class RagIndexError(ValueError):
    """Stable failure without paths, corpus text, or dependency diagnostics."""


class NamedEmbeddingProvider(Protocol):
    model_name: str
    backend_name: str
    backend_version: str

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        ...


class FastEmbedProvider:
    model_name = EMBEDDING_MODEL
    backend_name = "fastembed"

    def __init__(self, *, cache_dir: Path | None = None) -> None:
        try:
            text_embedding = importlib.import_module("fastembed").TextEmbedding
        except ImportError as exc:
            raise RagIndexError("rag_extra_missing") from exc
        try:
            self.backend_version = version("fastembed")
        except PackageNotFoundError as exc:
            raise RagIndexError("rag_extra_missing") from exc
        try:
            self._backend = text_embedding(
                model_name=self.model_name,
                cache_dir=str(cache_dir) if cache_dir is not None else None,
                lazy_load=True,
            )
        except Exception as exc:
            raise RagIndexError("embedding_model_unavailable") from exc

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts or not all(isinstance(text, str) and text for text in texts):
            raise RagIndexError("embedding_input_invalid")
        try:
            vectors = tuple(
                tuple(float(value) for value in vector)
                for vector in self._backend.embed(list(texts))
            )
        except Exception as exc:
            raise RagIndexError("embedding_failed") from exc
        _validate_vectors(vectors, expected_count=len(texts))
        return vectors


@dataclass(frozen=True)
class IndexManifest:
    schema_version: str
    index_version: str
    corpus_hash: str
    chunk_hashes: tuple[str, ...]
    chunk_size: int
    overlap: int
    embedding_backend: str
    embedding_backend_version: str
    embedding_model: str
    embedding_probe_hash: str
    dimension: int
    distance: str
    point_count: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "index_version": self.index_version,
            "corpus_hash": self.corpus_hash,
            "chunk_hashes": list(self.chunk_hashes),
            "chunking": {"chunk_size": self.chunk_size, "overlap": self.overlap},
            "embedding_backend": {
                "name": self.embedding_backend,
                "version": self.embedding_backend_version,
            },
            "embedding_model": self.embedding_model,
            "embedding_probe_hash": self.embedding_probe_hash,
            "dimension": self.dimension,
            "distance": self.distance,
            "point_count": self.point_count,
            "created_at": self.created_at,
        }

    def public_provenance(self) -> dict[str, str | int]:
        return {
            "index_version": self.index_version,
            "corpus_hash": self.corpus_hash,
            "embedding_backend": self.embedding_backend,
            "embedding_backend_version": self.embedding_backend_version,
            "embedding_model": self.embedding_model,
            "embedding_probe_hash": self.embedding_probe_hash,
            "dimension": self.dimension,
            "distance": self.distance,
            "point_count": self.point_count,
        }

    @classmethod
    def create(
        cls,
        corpus: Corpus,
        *,
        embedding_backend: str,
        embedding_backend_version: str,
        embedding_model: str,
        embedding_probe_hash: str,
        dimension: int,
        created_at: str,
    ) -> "IndexManifest":
        identity = _identity_payload(
            corpus,
            embedding_backend=embedding_backend,
            embedding_backend_version=embedding_backend_version,
            embedding_model=embedding_model,
            embedding_probe_hash=embedding_probe_hash,
            dimension=dimension,
        )
        index_version = hashlib.sha256(_canonical(identity)).hexdigest()
        return cls(
            schema_version=SCHEMA_VERSION,
            index_version=index_version,
            corpus_hash=corpus.content_hash,
            chunk_hashes=corpus.chunk_hashes,
            chunk_size=corpus.chunk_size,
            overlap=corpus.overlap,
            embedding_backend=embedding_backend,
            embedding_backend_version=embedding_backend_version,
            embedding_model=embedding_model,
            embedding_probe_hash=embedding_probe_hash,
            dimension=dimension,
            distance=DISTANCE,
            point_count=len(corpus.chunks),
            created_at=created_at,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IndexManifest":
        if set(value) != _MANIFEST_KEYS:
            raise RagIndexError("index_manifest_invalid")
        chunking = value.get("chunking")
        embedding_backend = value.get("embedding_backend")
        chunk_hashes = value.get("chunk_hashes")
        if not isinstance(chunking, Mapping) or set(chunking) != {"chunk_size", "overlap"}:
            raise RagIndexError("index_manifest_invalid")
        if not isinstance(embedding_backend, Mapping) or set(embedding_backend) != {"name", "version"}:
            raise RagIndexError("index_manifest_invalid")
        if not isinstance(chunk_hashes, list) or not chunk_hashes:
            raise RagIndexError("index_manifest_invalid")
        manifest = cls(
            schema_version=_required_str(value, "schema_version"),
            index_version=_required_hash(value, "index_version"),
            corpus_hash=_required_hash(value, "corpus_hash"),
            chunk_hashes=tuple(_hash_value(item) for item in chunk_hashes),
            chunk_size=_required_int(chunking, "chunk_size", minimum=1),
            overlap=_required_int(chunking, "overlap", minimum=0),
            embedding_backend=_required_str(embedding_backend, "name"),
            embedding_backend_version=_required_str(embedding_backend, "version"),
            embedding_model=_required_str(value, "embedding_model"),
            embedding_probe_hash=_required_hash(value, "embedding_probe_hash"),
            dimension=_required_int(value, "dimension", minimum=1),
            distance=_required_str(value, "distance"),
            point_count=_required_int(value, "point_count", minimum=1),
            created_at=_required_str(value, "created_at"),
        )
        if manifest.schema_version != SCHEMA_VERSION or manifest.distance != DISTANCE:
            raise RagIndexError("index_manifest_invalid")
        if manifest.overlap >= manifest.chunk_size or manifest.point_count != len(manifest.chunk_hashes):
            raise RagIndexError("index_manifest_invalid")
        try:
            created = datetime.fromisoformat(manifest.created_at)
        except ValueError as exc:
            raise RagIndexError("index_manifest_invalid") from exc
        if created.tzinfo is None:
            raise RagIndexError("index_manifest_invalid")
        identity = {
            "schema_version": manifest.schema_version,
            "corpus_hash": manifest.corpus_hash,
            "chunk_hashes": list(manifest.chunk_hashes),
            "chunking": {"chunk_size": manifest.chunk_size, "overlap": manifest.overlap},
            "embedding_backend": {
                "name": manifest.embedding_backend,
                "version": manifest.embedding_backend_version,
            },
            "embedding_model": manifest.embedding_model,
            "embedding_probe_hash": manifest.embedding_probe_hash,
            "dimension": manifest.dimension,
            "distance": manifest.distance,
            "point_count": manifest.point_count,
        }
        if hashlib.sha256(_canonical(identity)).hexdigest() != manifest.index_version:
            raise RagIndexError("index_manifest_invalid")
        return manifest


class QdrantVectorSearch:
    def __init__(self, corpus: Corpus, index_dir: Path, embeddings: NamedEmbeddingProvider) -> None:
        self.corpus = corpus
        self.embeddings = embeddings
        index_dir = Path(index_dir)
        _validate_index_path(index_dir)
        self.manifest = _read_manifest(index_dir / MANIFEST_NAME)
        _validate_manifest(self.manifest, corpus, embeddings)
        self._client, _models = _open_qdrant(index_dir / "qdrant")
        try:
            _validate_collection(self._client, corpus, self.manifest)
        except Exception:
            self._client.close()
            raise

    @property
    def index_version(self) -> str:
        return self.manifest.index_version

    def search(self, query: str, *, top_k: int = 5) -> tuple[SearchHit, ...]:
        if not isinstance(query, str) or not query.strip():
            raise CorpusError("search query 必须是非空字符串")
        if top_k <= 0 or top_k > 10:
            raise CorpusError("search top_k 必须在 1..10")
        vector = self.embeddings.embed((query,))[0]
        if len(vector) != self.manifest.dimension:
            raise RagIndexError("index_embedding_dimension_mismatch")
        try:
            points = self._client.query_points(
                COLLECTION_NAME,
                query=list(vector),
                limit=min(top_k, self.manifest.point_count),
                with_payload=True,
                with_vectors=False,
            ).points
        except Exception as exc:
            raise RagIndexError("index_query_failed") from exc
        chunks = {chunk.id: chunk for chunk in self.corpus.chunks}
        source_names = {source.id: source.name for source in self.corpus.sources}
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for point in points:
            payload = point.payload
            if not isinstance(payload, dict) or set(payload) != {"chunk_id", "source_id"}:
                raise RagIndexError("index_payload_invalid")
            chunk_id = payload.get("chunk_id")
            source_id = payload.get("source_id")
            if not isinstance(chunk_id, str) or not isinstance(source_id, str):
                raise RagIndexError("index_payload_invalid")
            chunk = chunks.get(chunk_id)
            if chunk is None or source_id != chunk.source_id or chunk_id in seen:
                raise RagIndexError("index_payload_invalid")
            seen.add(chunk_id)
            hits.append(SearchHit(chunk_id, source_id, source_names[source_id], float(point.score)))
        return tuple(hits)

    def close(self) -> None:
        self._client.close()


def build_local_index(
    corpus: Corpus,
    index_dir: Path,
    embeddings: NamedEmbeddingProvider,
    *,
    rebuild: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IndexManifest:
    index_dir = Path(index_dir)
    _prepare_index_dir(index_dir, rebuild=rebuild)
    embedded = embeddings.embed((*tuple(chunk.text for chunk in corpus.chunks), EMBEDDING_PROBE))
    vectors = embedded[:-1]
    probe_hash = _vector_hash(embedded[-1])
    dimension = len(vectors[0])
    manifest = IndexManifest.create(
        corpus,
        embedding_backend=embeddings.backend_name,
        embedding_backend_version=embeddings.backend_version,
        embedding_model=embeddings.model_name,
        embedding_probe_hash=probe_hash,
        dimension=dimension,
        created_at=clock().astimezone(UTC).isoformat(),
    )
    client, models = _open_qdrant(index_dir / "qdrant")
    try:
        exists = client.collection_exists(COLLECTION_NAME)
        if exists and not rebuild:
            raise RagIndexError("index_exists")
        if exists:
            client.delete_collection(COLLECTION_NAME)
        client.create_collection(
            COLLECTION_NAME,
            vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
        )
        points = [
            models.PointStruct(
                id=index,
                vector=list(vector),
                payload={"chunk_id": chunk.id, "source_id": chunk.source_id},
            )
            for index, (chunk, vector) in enumerate(zip(corpus.chunks, vectors))
        ]
        client.upsert(COLLECTION_NAME, points=points, wait=True)
        _validate_collection(client, corpus, manifest)
    except RagIndexError:
        raise
    except Exception as exc:
        raise RagIndexError("index_build_failed") from exc
    finally:
        client.close()
    _write_manifest(index_dir / MANIFEST_NAME, manifest)
    return manifest


def verify_local_index(
    corpus: Corpus,
    index_dir: Path,
    embeddings: NamedEmbeddingProvider,
) -> IndexManifest:
    search = QdrantVectorSearch(corpus, index_dir, embeddings)
    try:
        return search.manifest
    finally:
        search.close()


def _prepare_index_dir(index_dir: Path, *, rebuild: bool) -> None:
    if index_dir.is_symlink():
        raise RagIndexError("index_path_invalid")
    try:
        index_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise RagIndexError("index_path_invalid") from exc
    if not index_dir.is_dir() or (index_dir / MANIFEST_NAME).is_symlink():
        raise RagIndexError("index_path_invalid")
    if (index_dir / MANIFEST_NAME).exists() and not rebuild:
        raise RagIndexError("index_exists")


def _validate_index_path(index_dir: Path) -> None:
    manifest = index_dir / MANIFEST_NAME
    if (
        index_dir.is_symlink()
        or not index_dir.is_dir()
        or manifest.is_symlink()
        or not manifest.is_file()
    ):
        raise RagIndexError("index_path_invalid")


def _identity_payload(
    corpus: Corpus,
    *,
    embedding_backend: str,
    embedding_backend_version: str,
    embedding_model: str,
    embedding_probe_hash: str,
    dimension: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "corpus_hash": corpus.content_hash,
        "chunk_hashes": list(corpus.chunk_hashes),
        "chunking": {"chunk_size": corpus.chunk_size, "overlap": corpus.overlap},
        "embedding_backend": {
            "name": embedding_backend,
            "version": embedding_backend_version,
        },
        "embedding_model": embedding_model,
        "embedding_probe_hash": embedding_probe_hash,
        "dimension": dimension,
        "distance": DISTANCE,
        "point_count": len(corpus.chunks),
    }


def _validate_manifest(
    manifest: IndexManifest,
    corpus: Corpus,
    embeddings: NamedEmbeddingProvider,
) -> None:
    probe = embeddings.embed((EMBEDDING_PROBE,))[0]
    if len(probe) != manifest.dimension:
        raise RagIndexError("index_stale")
    expected = IndexManifest.create(
        corpus,
        embedding_backend=embeddings.backend_name,
        embedding_backend_version=embeddings.backend_version,
        embedding_model=embeddings.model_name,
        embedding_probe_hash=_vector_hash(probe),
        dimension=manifest.dimension,
        created_at=manifest.created_at,
    )
    if manifest != expected:
        raise RagIndexError("index_stale")


def _validate_collection(client: Any, corpus: Corpus, manifest: IndexManifest) -> None:
    try:
        if not client.collection_exists(COLLECTION_NAME):
            raise RagIndexError("index_collection_missing")
        if client.count(COLLECTION_NAME, exact=True).count != manifest.point_count:
            raise RagIndexError("index_point_count_mismatch")
        records, offset = client.scroll(
            COLLECTION_NAME,
            limit=manifest.point_count + 1,
            with_payload=True,
            with_vectors=False,
        )
    except RagIndexError:
        raise
    except Exception as exc:
        raise RagIndexError("index_unavailable") from exc
    if offset is not None or len(records) != manifest.point_count:
        raise RagIndexError("index_point_count_mismatch")
    expected = {(chunk.id, chunk.source_id) for chunk in corpus.chunks}
    actual: set[tuple[str, str]] = set()
    for record in records:
        payload = record.payload
        if not isinstance(payload, dict) or set(payload) != {"chunk_id", "source_id"}:
            raise RagIndexError("index_payload_invalid")
        chunk_id = payload.get("chunk_id")
        source_id = payload.get("source_id")
        if not isinstance(chunk_id, str) or not isinstance(source_id, str):
            raise RagIndexError("index_payload_invalid")
        actual.add((chunk_id, source_id))
    if actual != expected:
        raise RagIndexError("index_payload_invalid")


def _open_qdrant(path: Path) -> tuple[Any, Any]:
    try:
        module = importlib.import_module("qdrant_client")
    except ImportError as exc:
        raise RagIndexError("rag_extra_missing") from exc
    try:
        return (
            module.QdrantClient(
                path=str(path),
                # The default local-mode probe leaves a temporary SQLite connection
                # unclosed on Python 3.14. This CLI path is synchronous and single-process.
                force_disable_check_same_thread=True,
            ),
            module.models,
        )
    except Exception as exc:
        raise RagIndexError("index_unavailable") from exc


def _read_manifest(path: Path) -> IndexManifest:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RagIndexError("index_manifest_invalid") from exc
    if not isinstance(value, Mapping):
        raise RagIndexError("index_manifest_invalid")
    return IndexManifest.from_dict(value)


def _write_manifest(path: Path, manifest: IndexManifest) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    payload = _canonical(manifest.to_dict()) + b"\n"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise RagIndexError("index_manifest_write_failed") from exc


def _validate_vectors(vectors: Sequence[Sequence[float]], *, expected_count: int) -> None:
    if len(vectors) != expected_count or not vectors:
        raise RagIndexError("embedding_count_mismatch")
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1 or 0 in dimensions:
        raise RagIndexError("embedding_dimension_invalid")
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        raise RagIndexError("embedding_value_invalid")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _vector_hash(vector: Sequence[float]) -> str:
    try:
        payload = struct.pack(f"!{len(vector)}d", *vector)
    except (OverflowError, struct.error) as exc:
        raise RagIndexError("embedding_value_invalid") from exc
    return hashlib.sha256(payload).hexdigest()


def _required_str(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RagIndexError("index_manifest_invalid")
    return item


def _required_int(value: Mapping[str, Any], key: str, *, minimum: int) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
        raise RagIndexError("index_manifest_invalid")
    return item


def _required_hash(value: Mapping[str, Any], key: str) -> str:
    return _hash_value(value.get(key))


def _hash_value(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RagIndexError("index_manifest_invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise RagIndexError("index_manifest_invalid") from exc
    return value
