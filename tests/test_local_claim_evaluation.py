"""Private local claim-suite contract tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from agentlab.research.local_claim_evaluation import (
    LocalClaimEvaluationError,
    validate_local_claim_suite,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _claim(claim_id: str, source: str, text: str, source_text: str) -> dict[str, object]:
    start = source_text.index(text)
    return {
        "id": claim_id,
        "text": text,
        "evidence": [
            {"source": source, "start": start, "end": start + len(text), "exact_text": text}
        ],
    }


def _case(
    case_id: str,
    *,
    category: str = "single_document",
    split: str = "calibration",
    answerability: str = "answerable",
    adjudication_status: str = "adjudicated",
    primary_reviewed: bool = True,
    independent_reviewed: bool = True,
    claims: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "split": split,
        "category": category,
        "query": f"Private query marker {case_id}?",
        "answerability": answerability,
        "adjudication_status": adjudication_status,
        "review": {
            "primary_reviewed": primary_reviewed,
            "independent_reviewed": independent_reviewed,
            "adjudication_note": None,
        },
        "ground_truth_claims": claims or [],
    }


def _build_suite(root: Path, *, frozen_shape: bool = False) -> tuple[Path, dict[str, str]]:
    suite = root / "local-suite"
    corpus_root = suite / "corpus"
    corpus_root.mkdir(parents=True)
    suite.chmod(0o700)
    corpus_root.chmod(0o700)
    texts = {
        "alpha.md": "Alpha status is ready. Alpha count is 7. Alpha mode is not remote.\n",
        "beta.md": "Beta status is paused. Beta date is 2026-08-13.\n",
    }
    for name, text in texts.items():
        path = corpus_root / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    alpha_claim = _claim("claim-alpha-ready", "alpha.md", "Alpha status is ready.", texts["alpha.md"])
    beta_claim = _claim("claim-beta-paused", "beta.md", "Beta status is paused.", texts["beta.md"])
    if not frozen_shape:
        cases = [_case("case-alpha", claims=[alpha_claim])]
    else:
        categories = [
            "single_document",
            "multi_document",
            "numeric_temporal",
            "negation",
            "unanswerable",
            "bilingual_rewrite",
        ]
        cases = []
        for index in range(24):
            category = categories[index] if index < len(categories) else "single_document"
            split = "calibration" if index < 16 else "holdout"
            if category == "unanswerable":
                case = _case(
                    f"case-{index}",
                    category=category,
                    split=split,
                    answerability="unanswerable",
                )
            elif category == "multi_document":
                case = _case(
                    f"case-{index}",
                    category=category,
                    split=split,
                    claims=[alpha_claim, beta_claim],
                )
            elif category == "numeric_temporal":
                case = _case(
                    f"case-{index}",
                    category=category,
                    split=split,
                    claims=[
                        _claim(
                            "claim-alpha-count",
                            "alpha.md",
                            "Alpha count is 7.",
                            texts["alpha.md"],
                        )
                    ],
                )
            elif category == "negation":
                case = _case(
                    f"case-{index}",
                    category=category,
                    split=split,
                    claims=[
                        _claim(
                            "claim-alpha-not-remote",
                            "alpha.md",
                            "Alpha mode is not remote.",
                            texts["alpha.md"],
                        )
                    ],
                )
            else:
                case = _case(
                    f"case-{index}",
                    category=category,
                    split=split,
                    claims=[alpha_claim],
                )
            isolated_case = json.loads(json.dumps(case))
            for claim_index, claim in enumerate(isolated_case["ground_truth_claims"]):
                claim["id"] = f"claim-{index}-{claim_index}"
            cases.append(isolated_case)

    cases_path = suite / "cases-v1.json"
    _write_json(cases_path, {"schema_version": 1, "cases": cases})
    manifest = {
        "schema_version": 1,
        "suite_id": "private-agent-docs-pilot",
        "suite_version": 1,
        "status": "draft",
        "provenance": "authorized_private",
        "source_snapshot": "synthetic-test-fixture-v1",
        "annotation_protocol_version": "local-claim-annotation-v1",
        "corpus": {
            "directory": "corpus",
            "sha256": "pending",
            "chunk_size": 256,
            "overlap": 32,
            "chunks_sha256": "pending",
        },
        "cases": {"file": "cases-v1.json", "sha256": "pending"},
    }
    _write_json(suite / "manifest-v1.json", manifest)
    report = validate_local_claim_suite(suite)
    return suite, {
        "corpus": report.corpus_sha256,
        "cases": report.cases_sha256,
        "chunks": report.chunks_sha256,
    }


def _freeze(suite: Path, hashes: dict[str, str]) -> None:
    manifest_path = suite / "manifest-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "frozen"
    manifest["corpus"]["sha256"] = hashes["corpus"]
    manifest["corpus"]["chunks_sha256"] = hashes["chunks"]
    manifest["cases"]["sha256"] = hashes["cases"]
    _write_json(manifest_path, manifest)


def test_draft_suite_returns_redacted_reproducibility_hashes(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    report = validate_local_claim_suite(suite)
    rendered = json.dumps(report.to_dict(), ensure_ascii=False)
    assert report.status == "draft"
    assert report.source_snapshot == "synthetic-test-fixture-v1"
    assert report.frozen_ready is False
    assert report.case_count == report.claim_count == report.evidence_span_count == 1
    assert "Private query marker" not in rendered
    assert "Alpha status is ready" not in rendered
    assert str(suite) not in rendered


def test_complete_suite_can_be_frozen_with_bound_hashes(tmp_path: Path) -> None:
    suite, hashes = _build_suite(tmp_path, frozen_shape=True)
    draft = validate_local_claim_suite(suite)
    assert draft.frozen_ready is True
    _freeze(suite, hashes)
    frozen = validate_local_claim_suite(suite)
    assert frozen.status == "frozen"
    assert frozen.case_count == 24
    assert frozen.split_counts == {"calibration": 16, "holdout": 8}
    assert frozen.frozen_ready is True


def test_conflict_is_allowed_but_not_required_for_freeze(tmp_path: Path) -> None:
    suite, hashes = _build_suite(tmp_path, frozen_shape=True)
    report = validate_local_claim_suite(suite)
    assert "conflict" not in report.category_counts
    assert report.frozen_ready is True

    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    value["cases"][0]["category"] = "conflict"
    value["cases"][0]["answerability"] = "conflicting"
    value["cases"][0]["ground_truth_claims"].append(
        _claim(
            "claim-conflict-beta",
            "beta.md",
            "Beta status is paused.",
            "Beta status is paused. Beta date is 2026-08-13.\n",
        )
    )
    _write_json(cases_path, value)
    hashes["cases"] = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    report = validate_local_claim_suite(suite)
    assert report.category_counts["conflict"] == 1
    assert report.frozen_ready is True


def test_frozen_suite_rejects_case_hash_drift(tmp_path: Path) -> None:
    suite, hashes = _build_suite(tmp_path, frozen_shape=True)
    _freeze(suite, hashes)
    cases = suite / "cases-v1.json"
    cases.write_text(cases.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    cases.chmod(0o600)
    with pytest.raises(LocalClaimEvaluationError, match="cases hash mismatch"):
        validate_local_claim_suite(suite)


def test_span_must_match_source_and_fit_one_chunk(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    evidence = value["cases"][0]["ground_truth_claims"][0]["evidence"][0]
    evidence["exact_text"] = "Alpha status is wrong."
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="does not match"):
        validate_local_claim_suite(suite)

    suite, _hashes = _build_suite(tmp_path / "crossing")
    manifest_path = suite / "manifest-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corpus"]["chunk_size"] = 24
    manifest["corpus"]["overlap"] = 0
    _write_json(manifest_path, manifest)
    value = json.loads((suite / "cases-v1.json").read_text(encoding="utf-8"))
    claim = value["cases"][0]["ground_truth_claims"][0]
    claim["evidence"][0] = {
        "source": "alpha.md",
        "start": 0,
        "end": 40,
        "exact_text": "Alpha status is ready. Alpha count is 7.",
    }
    _write_json(suite / "cases-v1.json", value)
    with pytest.raises(LocalClaimEvaluationError, match="crosses"):
        validate_local_claim_suite(suite)


def test_duplicate_query_and_unknown_fields_fail_closed(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    duplicate = json.loads(json.dumps(value["cases"][0]))
    duplicate["id"] = "case-beta"
    duplicate["ground_truth_claims"][0]["id"] = "claim-beta-ready"
    value["cases"].append(duplicate)
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="duplicate query"):
        validate_local_claim_suite(suite)

    value["cases"].pop()
    value["cases"][0]["notes"] = "must stay private"
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="fields invalid"):
        validate_local_claim_suite(suite)


def test_claim_ids_are_unique_across_suite_and_queries_are_normalized(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    duplicate = json.loads(json.dumps(value["cases"][0]))
    duplicate["id"] = "case-beta"
    duplicate["query"] = "  private   QUERY marker case-alpha?  "
    value["cases"].append(duplicate)
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="duplicate claim id"):
        validate_local_claim_suite(suite)

    duplicate["ground_truth_claims"][0]["id"] = "claim-beta-ready"
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="duplicate query"):
        validate_local_claim_suite(suite)


def test_frozen_suite_rejects_pending_review(tmp_path: Path) -> None:
    suite, hashes = _build_suite(tmp_path, frozen_shape=True)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    value["cases"][0]["adjudication_status"] = "needs_review"
    value["cases"][0]["review"]["independent_reviewed"] = False
    _write_json(cases_path, value)
    hashes["cases"] = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    _freeze(suite, hashes)
    with pytest.raises(LocalClaimEvaluationError, match="freeze gates"):
        validate_local_claim_suite(suite)


def test_adjudicated_case_requires_two_recorded_reviews(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    value["cases"][0]["review"]["independent_reviewed"] = False
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="two recorded reviews"):
        validate_local_claim_suite(suite)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("primary_reviewed", 1, "primary review invalid"),
        ("independent_reviewed", "yes", "independent review invalid"),
        ("adjudication_note", "", "adjudication note invalid"),
    ],
)
def test_review_record_is_strict(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases["cases"][0]["review"][field] = value
    _write_json(cases_path, cases)
    with pytest.raises(LocalClaimEvaluationError, match=message):
        validate_local_claim_suite(suite)


@pytest.mark.parametrize(
    ("answerability", "claims", "message"),
    [
        ("answerable", [], "requires Gold claims"),
        ("unanswerable", "existing", "must not contain Gold claims"),
        ("conflicting", "existing", "at least two Gold claims"),
    ],
)
def test_answerability_and_gold_shape_are_consistent(
    tmp_path: Path, answerability: str, claims: object, message: str
) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    case = value["cases"][0]
    case["answerability"] = answerability
    if claims == []:
        case["ground_truth_claims"] = []
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match=message):
        validate_local_claim_suite(suite)


def test_frozen_manifest_rejects_pending_hashes(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path, frozen_shape=True)
    manifest_path = suite / "manifest-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "frozen"
    _write_json(manifest_path, manifest)
    with pytest.raises(LocalClaimEvaluationError, match="corpus hash invalid"):
        validate_local_claim_suite(suite)


def test_manifest_requires_source_snapshot(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    manifest_path = suite / "manifest-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["source_snapshot"]
    _write_json(manifest_path, manifest)
    with pytest.raises(LocalClaimEvaluationError, match="manifest fields invalid"):
        validate_local_claim_suite(suite)


@pytest.mark.parametrize("snapshot", ["/private/path", "contains spaces", "A" * 161])
def test_source_snapshot_is_public_safe(tmp_path: Path, snapshot: str) -> None:
    suite, _hashes = _build_suite(tmp_path)
    manifest_path = suite / "manifest-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_snapshot"] = snapshot
    _write_json(manifest_path, manifest)
    with pytest.raises(LocalClaimEvaluationError, match="source snapshot invalid"):
        validate_local_claim_suite(suite)


@pytest.mark.parametrize("source", ["../outside.md", "/private/source.md"])
def test_evidence_source_must_be_normalized_relative_path(
    tmp_path: Path, source: str
) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    value["cases"][0]["ground_truth_claims"][0]["evidence"][0]["source"] = source
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="source path invalid"):
        validate_local_claim_suite(suite)


def test_evidence_must_reference_loaded_source(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    cases_path = suite / "cases-v1.json"
    value = json.loads(cases_path.read_text(encoding="utf-8"))
    value["cases"][0]["ground_truth_claims"][0]["evidence"][0]["source"] = "missing.md"
    _write_json(cases_path, value)
    with pytest.raises(LocalClaimEvaluationError, match="unknown source"):
        validate_local_claim_suite(suite)


def test_permissions_unknown_members_and_symlinks_fail_closed(tmp_path: Path) -> None:
    suite, _hashes = _build_suite(tmp_path)
    (suite / "cases-v1.json").chmod(0o644)
    with pytest.raises(LocalClaimEvaluationError, match="file permissions"):
        validate_local_claim_suite(suite)

    suite, _hashes = _build_suite(tmp_path / "unknown")
    extra = suite / "review-notes.txt"
    extra.write_text("private", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(LocalClaimEvaluationError, match="root members"):
        validate_local_claim_suite(suite)

    suite, _hashes = _build_suite(tmp_path / "symlink")
    target = tmp_path / "outside.md"
    target.write_text("outside", encoding="utf-8")
    os.symlink(target, suite / "corpus" / "linked.md")
    with pytest.raises(LocalClaimEvaluationError, match="unsafe file"):
        validate_local_claim_suite(suite)
