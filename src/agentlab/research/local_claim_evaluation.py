"""Strict local contracts for a private claim suite with explicit review state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .corpus import Corpus, CorpusError


SCHEMA_VERSION = 1
ANNOTATION_PROTOCOL_VERSION = "local-claim-annotation-v1"
MANIFEST_FILE = "manifest-v1.json"
CASES_FILE = "cases-v1.json"
CORPUS_DIRECTORY = "corpus"
MAX_JSON_BYTES = 8 * 1024 * 1024
MIN_FROZEN_CASES = 24
MIN_CALIBRATION_CASES = 16
MIN_HOLDOUT_CASES = 8
ALLOWED_CATEGORIES = frozenset({
    "single_document",
    "multi_document",
    "numeric_temporal",
    "negation",
    "conflict",
    "unanswerable",
    "bilingual_rewrite",
})
REQUIRED_CATEGORIES = ALLOWED_CATEGORIES - {"conflict"}
_KEBAB_CASE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SNAPSHOT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class LocalClaimEvaluationError(ValueError):
    """Stable error for a malformed, unsafe, or stale local suite."""


@dataclass(frozen=True)
class EvidenceSpan:
    source: str
    start: int
    end: int
    exact_text: str


@dataclass(frozen=True)
class GroundTruthClaim:
    id: str
    text: str
    evidence: tuple[EvidenceSpan, ...]


@dataclass(frozen=True)
class LocalClaimCase:
    id: str
    split: str
    category: str
    query: str
    answerability: str
    adjudication_status: str
    primary_reviewed: bool
    independent_reviewed: bool
    adjudication_note: str | None
    ground_truth_claims: tuple[GroundTruthClaim, ...]


@dataclass(frozen=True)
class LocalClaimManifest:
    suite_id: str
    suite_version: int
    status: str
    provenance: str
    source_snapshot: str
    corpus_sha256: str
    chunk_size: int
    overlap: int
    chunks_sha256: str
    cases_sha256: str


@dataclass(frozen=True)
class LocalClaimValidationReport:
    suite_id: str
    suite_version: int
    status: str
    provenance: str
    source_snapshot: str
    case_count: int
    claim_count: int
    evidence_span_count: int
    split_counts: Mapping[str, int]
    category_counts: Mapping[str, int]
    corpus_sha256: str
    cases_sha256: str
    chunks_sha256: str
    frozen_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "status": self.status,
            "provenance": self.provenance,
            "source_snapshot": self.source_snapshot,
            "case_count": self.case_count,
            "claim_count": self.claim_count,
            "evidence_span_count": self.evidence_span_count,
            "split_counts": dict(sorted(self.split_counts.items())),
            "category_counts": dict(sorted(self.category_counts.items())),
            "corpus_sha256": self.corpus_sha256,
            "cases_sha256": self.cases_sha256,
            "chunks_sha256": self.chunks_sha256,
            "frozen_ready": self.frozen_ready,
            "boundary": (
                "local structural and reproducibility validation only; no source, query, Gold text, "
                "Provider call, semantic-label correctness, or model-quality claim"
            ),
        }


def validate_local_claim_suite(root: Path) -> LocalClaimValidationReport:
    root = Path(root)
    _validate_private_tree(root)
    _validate_root_members(root)
    manifest = _parse_manifest(_read_json(root / MANIFEST_FILE, "manifest"))
    raw_cases = _read_bytes(root / CASES_FILE, "cases")
    cases = _parse_cases(_decode_json(raw_cases, "cases"))
    try:
        corpus = Corpus.load(
            root / CORPUS_DIRECTORY,
            chunk_size=manifest.chunk_size,
            overlap=manifest.overlap,
        )
    except CorpusError as exc:
        raise LocalClaimEvaluationError("local claim corpus invalid") from exc

    corpus_sha256 = _corpus_digest(corpus)
    cases_sha256 = hashlib.sha256(raw_cases).hexdigest()
    chunks_sha256 = _chunk_digest(corpus)
    _validate_hash("corpus", manifest.corpus_sha256, corpus_sha256, manifest.status)
    _validate_hash("cases", manifest.cases_sha256, cases_sha256, manifest.status)
    _validate_hash("chunks", manifest.chunks_sha256, chunks_sha256, manifest.status)
    _validate_cases_against_corpus(cases, corpus)
    frozen_ready = _is_frozen_ready(cases)
    if manifest.status == "frozen" and not frozen_ready:
        raise LocalClaimEvaluationError("frozen local claim suite does not meet freeze gates")

    split_counts = Counter(case.split for case in cases)
    category_counts = Counter(case.category for case in cases)
    return LocalClaimValidationReport(
        suite_id=manifest.suite_id,
        suite_version=manifest.suite_version,
        status=manifest.status,
        provenance=manifest.provenance,
        source_snapshot=manifest.source_snapshot,
        case_count=len(cases),
        claim_count=sum(len(case.ground_truth_claims) for case in cases),
        evidence_span_count=sum(
            len(claim.evidence) for case in cases for claim in case.ground_truth_claims
        ),
        split_counts=split_counts,
        category_counts=category_counts,
        corpus_sha256=corpus_sha256,
        cases_sha256=cases_sha256,
        chunks_sha256=chunks_sha256,
        frozen_ready=frozen_ready,
    )


def _validate_private_tree(root: Path) -> None:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise LocalClaimEvaluationError("local claim suite root unreadable") from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise LocalClaimEvaluationError("local claim suite root must be a regular directory")
    for current_root, directory_names, file_names, root_fd in os.fwalk(
        root, topdown=True, follow_symlinks=False
    ):
        current = Path(current_root)
        _require_mode(os.fstat(root_fd).st_mode, 0o700, "directory")
        for name in directory_names:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise LocalClaimEvaluationError("local claim suite contains unsafe directory")
        for name in file_names:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise LocalClaimEvaluationError("local claim suite contains unsafe file")
            _require_mode(info.st_mode, 0o600, "file")
        if current != root and current.name.startswith("."):
            raise LocalClaimEvaluationError("local claim suite contains hidden directory")


def _require_mode(mode: int, expected: int, label: str) -> None:
    if stat.S_IMODE(mode) != expected:
        raise LocalClaimEvaluationError(f"local claim suite {label} permissions invalid")


def _validate_root_members(root: Path) -> None:
    expected = {MANIFEST_FILE, CASES_FILE, CORPUS_DIRECTORY}
    try:
        observed = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise LocalClaimEvaluationError("local claim suite root unreadable") from exc
    if observed != expected:
        raise LocalClaimEvaluationError("local claim suite root members invalid")


def _parse_manifest(value: Mapping[str, Any]) -> LocalClaimManifest:
    _require_keys(
        value,
        {
            "schema_version",
            "suite_id",
            "suite_version",
            "status",
            "provenance",
            "source_snapshot",
            "annotation_protocol_version",
            "corpus",
            "cases",
        },
        "manifest",
    )
    if value["schema_version"] != SCHEMA_VERSION:
        raise LocalClaimEvaluationError("manifest schema version invalid")
    if value["annotation_protocol_version"] != ANNOTATION_PROTOCOL_VERSION:
        raise LocalClaimEvaluationError("annotation protocol version invalid")
    suite_id = _require_kebab(value["suite_id"], "suite id")
    suite_version = _require_positive_int(value["suite_version"], "suite version")
    status = _require_choice(value["status"], {"draft", "frozen"}, "suite status")
    provenance = _require_choice(
        value["provenance"], {"public", "authorized_private"}, "suite provenance"
    )
    source_snapshot = _require_source_snapshot(value["source_snapshot"])
    corpus = _require_mapping(value["corpus"], "manifest corpus")
    _require_keys(
        corpus,
        {"directory", "sha256", "chunk_size", "overlap", "chunks_sha256"},
        "manifest corpus",
    )
    cases = _require_mapping(value["cases"], "manifest cases")
    _require_keys(cases, {"file", "sha256"}, "manifest cases")
    if corpus["directory"] != CORPUS_DIRECTORY or cases["file"] != CASES_FILE:
        raise LocalClaimEvaluationError("manifest local paths invalid")
    chunk_size = _require_positive_int(corpus["chunk_size"], "chunk size")
    overlap = _require_nonnegative_int(corpus["overlap"], "chunk overlap")
    if overlap >= chunk_size:
        raise LocalClaimEvaluationError("chunk overlap must be smaller than chunk size")
    return LocalClaimManifest(
        suite_id=suite_id,
        suite_version=suite_version,
        status=status,
        provenance=provenance,
        source_snapshot=source_snapshot,
        corpus_sha256=_require_hash_or_pending(corpus["sha256"], status, "corpus hash"),
        chunk_size=chunk_size,
        overlap=overlap,
        chunks_sha256=_require_hash_or_pending(
            corpus["chunks_sha256"], status, "chunk set hash"
        ),
        cases_sha256=_require_hash_or_pending(cases["sha256"], status, "cases hash"),
    )


def _parse_cases(value: Mapping[str, Any]) -> tuple[LocalClaimCase, ...]:
    _require_keys(value, {"schema_version", "cases"}, "cases")
    if value["schema_version"] != SCHEMA_VERSION:
        raise LocalClaimEvaluationError("cases schema version invalid")
    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise LocalClaimEvaluationError("cases must be a non-empty list")
    cases = tuple(_parse_case(_require_mapping(item, "case")) for item in raw_cases)
    _require_unique((case.id for case in cases), "case id")
    _require_unique(
        (claim.id for case in cases for claim in case.ground_truth_claims),
        "claim id",
    )
    normalized_queries = tuple(" ".join(case.query.split()).casefold() for case in cases)
    _require_unique(normalized_queries, "query")
    return cases


def _parse_case(value: Mapping[str, Any]) -> LocalClaimCase:
    _require_keys(
        value,
        {
            "id",
            "split",
            "category",
            "query",
            "answerability",
            "adjudication_status",
            "review",
            "ground_truth_claims",
        },
        "case",
    )
    query = _require_nonempty_text(value["query"], "query")
    raw_claims = value["ground_truth_claims"]
    if not isinstance(raw_claims, list):
        raise LocalClaimEvaluationError("ground truth claims invalid")
    claims = tuple(_parse_claim(_require_mapping(item, "ground truth claim")) for item in raw_claims)
    _require_unique((claim.id for claim in claims), "claim id within case")
    review = _require_mapping(value["review"], "case review")
    _require_keys(
        review,
        {"primary_reviewed", "independent_reviewed", "adjudication_note"},
        "case review",
    )
    primary_reviewed = _require_bool(review["primary_reviewed"], "primary review")
    independent_reviewed = _require_bool(
        review["independent_reviewed"], "independent review"
    )
    adjudication_note = review["adjudication_note"]
    if adjudication_note is not None and (
        not isinstance(adjudication_note, str) or not adjudication_note.strip()
    ):
        raise LocalClaimEvaluationError("adjudication note invalid")
    case = LocalClaimCase(
        id=_require_kebab(value["id"], "case id"),
        split=_require_choice(value["split"], {"calibration", "holdout"}, "case split"),
        category=_require_choice(value["category"], ALLOWED_CATEGORIES, "case category"),
        query=query,
        answerability=_require_choice(
            value["answerability"],
            {"answerable", "unanswerable", "conflicting", "unclear"},
            "case answerability",
        ),
        adjudication_status=_require_choice(
            value["adjudication_status"],
            {"needs_review", "adjudicated"},
            "case adjudication status",
        ),
        primary_reviewed=primary_reviewed,
        independent_reviewed=independent_reviewed,
        adjudication_note=adjudication_note,
        ground_truth_claims=claims,
    )
    if case.adjudication_status == "adjudicated" and not (
        case.primary_reviewed and case.independent_reviewed
    ):
        raise LocalClaimEvaluationError("adjudicated case requires two recorded reviews")
    if case.answerability == "unanswerable" and claims:
        raise LocalClaimEvaluationError("unanswerable case must not contain Gold claims")
    if case.answerability == "answerable" and not claims:
        raise LocalClaimEvaluationError("answerable case requires Gold claims")
    if case.answerability == "conflicting" and len(claims) < 2:
        raise LocalClaimEvaluationError("conflicting case requires at least two Gold claims")
    if case.category == "unanswerable" and case.answerability != "unanswerable":
        raise LocalClaimEvaluationError("unanswerable category requires unanswerable case")
    if case.category == "conflict" and case.answerability != "conflicting":
        raise LocalClaimEvaluationError("conflict category requires conflicting case")
    return case


def _parse_claim(value: Mapping[str, Any]) -> GroundTruthClaim:
    _require_keys(value, {"id", "text", "evidence"}, "ground truth claim")
    raw_evidence = value["evidence"]
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise LocalClaimEvaluationError("ground truth claim requires evidence")
    return GroundTruthClaim(
        id=_require_kebab(value["id"], "claim id"),
        text=_require_nonempty_text(value["text"], "claim text"),
        evidence=tuple(
            _parse_evidence(_require_mapping(item, "evidence span")) for item in raw_evidence
        ),
    )


def _parse_evidence(value: Mapping[str, Any]) -> EvidenceSpan:
    _require_keys(value, {"source", "start", "end", "exact_text"}, "evidence span")
    source = _require_nonempty_text(value["source"], "evidence source")
    path = Path(source)
    if path.is_absolute() or ".." in path.parts or source != path.as_posix():
        raise LocalClaimEvaluationError("evidence source path invalid")
    start = _require_nonnegative_int(value["start"], "evidence start")
    end = _require_positive_int(value["end"], "evidence end")
    exact_text = _require_nonempty_text(value["exact_text"], "evidence exact text")
    if end <= start:
        raise LocalClaimEvaluationError("evidence span invalid")
    return EvidenceSpan(source=source, start=start, end=end, exact_text=exact_text)


def _validate_cases_against_corpus(cases: Sequence[LocalClaimCase], corpus: Corpus) -> None:
    sources = {source.name: source for source in corpus.sources}
    chunks_by_source: dict[str, list[tuple[int, int]]] = {}
    source_names = {source.id: source.name for source in corpus.sources}
    for chunk in corpus.chunks:
        chunks_by_source.setdefault(source_names[chunk.source_id], []).append((chunk.start, chunk.end))
    for case in cases:
        referenced_sources: set[str] = set()
        for claim in case.ground_truth_claims:
            for evidence in claim.evidence:
                source = sources.get(evidence.source)
                if source is None:
                    raise LocalClaimEvaluationError("evidence references unknown source")
                if source.text[evidence.start:evidence.end] != evidence.exact_text:
                    raise LocalClaimEvaluationError("evidence span does not match source")
                if not any(
                    start <= evidence.start and evidence.end <= end
                    for start, end in chunks_by_source[evidence.source]
                ):
                    raise LocalClaimEvaluationError("evidence span crosses deterministic chunks")
                referenced_sources.add(evidence.source)
        if case.category == "single_document" and len(referenced_sources) != 1:
            raise LocalClaimEvaluationError("single-document case must cite exactly one source")
        if case.category == "multi_document" and len(referenced_sources) < 2:
            raise LocalClaimEvaluationError("multi-document case must cite at least two sources")
        if case.answerability == "conflicting" and len(referenced_sources) < 2:
            raise LocalClaimEvaluationError("conflicting case must cite at least two sources")


def _is_frozen_ready(cases: Sequence[LocalClaimCase]) -> bool:
    split_counts = Counter(case.split for case in cases)
    categories = {case.category for case in cases}
    return (
        len(cases) >= MIN_FROZEN_CASES
        and split_counts["calibration"] >= MIN_CALIBRATION_CASES
        and split_counts["holdout"] >= MIN_HOLDOUT_CASES
        and REQUIRED_CATEGORIES <= categories
        and all(case.adjudication_status == "adjudicated" for case in cases)
        and all(case.primary_reviewed and case.independent_reviewed for case in cases)
        and all(case.answerability != "unclear" for case in cases)
    )


def _corpus_digest(corpus: Corpus) -> str:
    value = [{"name": source.name, "text": source.text} for source in corpus.sources]
    return _canonical_digest(value)


def _chunk_digest(corpus: Corpus) -> str:
    source_names = {source.id: source.name for source in corpus.sources}
    value = [
        {
            "id": chunk.id,
            "source": source_names[chunk.source_id],
            "start": chunk.start,
            "end": chunk.end,
            "text_sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
        }
        for chunk in corpus.chunks
    ]
    return _canonical_digest(value)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_hash(label: str, expected: str, actual: str, status: str) -> None:
    if expected == "pending" and status == "draft":
        return
    if expected != actual:
        raise LocalClaimEvaluationError(f"local claim {label} hash mismatch")


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    return _decode_json(_read_bytes(path, label), label)


def _read_bytes(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if not getattr(os, "O_NOFOLLOW", 0):
        raise LocalClaimEvaluationError("platform cannot safely read local claim suite")
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise LocalClaimEvaluationError(f"local claim {label} unreadable") from exc
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise LocalClaimEvaluationError(f"local claim {label} unsafe")
        if info.st_size > MAX_JSON_BYTES:
            raise LocalClaimEvaluationError(f"local claim {label} too large")
        content = bytearray()
        while len(content) <= MAX_JSON_BYTES:
            block = os.read(file_fd, min(64 * 1024, MAX_JSON_BYTES + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > MAX_JSON_BYTES:
            raise LocalClaimEvaluationError(f"local claim {label} too large")
        return bytes(content)
    except OSError as exc:
        raise LocalClaimEvaluationError(f"local claim {label} unreadable") from exc
    finally:
        os.close(file_fd)


def _decode_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalClaimEvaluationError(f"local claim {label} invalid JSON") from exc
    return _require_mapping(value, label)


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalClaimEvaluationError(f"{label} must be an object")
    return value


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise LocalClaimEvaluationError(f"{label} fields invalid")


def _require_choice(value: object, choices: set[str] | frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise LocalClaimEvaluationError(f"{label} invalid")
    return value


def _require_nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalClaimEvaluationError(f"{label} invalid")
    return value


def _require_kebab(value: object, label: str) -> str:
    if not isinstance(value, str) or _KEBAB_CASE.fullmatch(value) is None:
        raise LocalClaimEvaluationError(f"{label} must be kebab-case")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LocalClaimEvaluationError(f"{label} invalid")
    return value


def _require_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LocalClaimEvaluationError(f"{label} invalid")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise LocalClaimEvaluationError(f"{label} invalid")
    return value


def _require_hash_or_pending(value: object, status: str, label: str) -> str:
    if value == "pending" and status == "draft":
        return value
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise LocalClaimEvaluationError(f"{label} invalid")
    return value


def _require_source_snapshot(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 160
        or _SOURCE_SNAPSHOT.fullmatch(value) is None
    ):
        raise LocalClaimEvaluationError("source snapshot invalid")
    return value


def _require_unique(values: Sequence[str] | Any, label: str) -> None:
    materialized = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise LocalClaimEvaluationError(f"duplicate {label}")
