"""Declarative evaluation suite contract and runner.

``research/evaluation.py`` holds the first suite, which is deliberately frozen:
its file digest, case count, category census and provenance split are constants
in the module, and changing any of them would require a version bump. That is
the right shape for a single pinned regression fixture -- and the wrong shape for
every other suite, because adding a suite should not mean editing the runner.

This module generalises the same evaluation into a **declarative contract**: a
suite directory carries its own ``suite.json`` describing corpus digest, case
census, thresholds and provenance, and the runner validates the artefacts against
that description. The scoring logic mirrors the frozen runner on purpose so the
two suites stay comparable.

Boundary: every suite here is a deterministic offline fixture. It is not
evidence of model quality, user scale, or business outcome.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..reasoning import Action
from .agent import ResearchAgent
from .artifacts import Citation, Claim, ResearchAnswer, ResearchValidationError
from .corpus import Corpus

SUITE_SCHEMA = 1
SUITE_FILENAME = "suite.json"
CORPUS_DIRNAME = "corpus"
BOUNDARY = "deterministic offline fixture; not model quality, user scale, or business outcome evidence"

_REQUIRED_TOP_LEVEL = {
    "suite_id",
    "suite_schema",
    "corpus_sha256",
    "provenance",
    "expectations",
    "thresholds",
    "cases",
}
_REQUIRED_CASE = {"id", "category", "provenance", "question", "gold_sources", "expected_status"}
_OPTIONAL_CASE = {"gold_span", "source_qa_id", "forbid_tools", "expected_side_effects", "fault"}
# 每个 fault 必须到达的终态。runner 无法自动判断"fault 是否真的走到了预期分支"，
# 所以由测试逐条断言 —— 否则 fault 可能只是把步数耗尽，让 case 以错误的理由通过。
FAULT_FINISH_REASONS = {
    "unknown_tool": "tool_failure",
    "invalid_arguments": "tool_failure",
    "path_traversal": "tool_failure",
    "step_budget": "step_budget_exhausted",
    "fabricated_citation": "validation_failed",
}
FAULTS = frozenset(
    {
        "unknown_tool",
        "invalid_arguments",
        "step_budget",
        "path_traversal",
        "fabricated_citation",
    }
)
_VALID_STATUS = {"answered", "insufficient_evidence"}


class SuiteError(ValueError):
    """Stable suite error; messages never include local paths or content."""


class _AdversarialReasoner:
    """Drive the real tool loop with a hostile action instead of a benign one.

    Each fault is an attempt to step outside the corpus contract: call a tool
    that was never registered, smuggle a filesystem path through ``chunk_id``,
    or return more bytes than the tool declared. The runner asserts on the
    observed outcome, so a defence regression fails the case rather than
    silently passing.
    """

    def __init__(self, fault: str) -> None:
        self.fault = fault

    def decide(self, *, task: str, context: tuple[str, ...], available_tools: tuple[str, ...]) -> Action:
        by_tool: dict[str, list[Any]] = {}
        for item in context:
            payload = item.split("观察: ", 1)[-1]
            if not payload.startswith("{"):
                continue
            try:
                observation = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(observation, dict) and "tool" in observation:
                by_tool.setdefault(str(observation["tool"]), []).append(observation.get("data"))
        if self.fault == "step_budget":
            return Action(kind="tool", tool="list_sources", argument="{}")
        if any("error:" in item for item in context):
            return Action(kind="finish", answer=ResearchAnswer.refused("tool_failure").to_json())
        if self.fault == "unknown_tool":
            return Action(kind="tool", tool="write_file", argument="{}")
        if self.fault == "invalid_arguments":
            return Action(kind="tool", tool="search_corpus", argument="{bad-json")
        if self.fault == "path_traversal":
            return Action(
                kind="tool",
                tool="read_chunk",
                argument=json.dumps({"chunk_id": "../../../etc/passwd"}, ensure_ascii=False),
            )
        if self.fault == "fabricated_citation":
            # 完整走一遍正常流程（list_sources -> search_corpus -> read_chunk），
            # 再用一个 span 合法、但文本被篡改的引用收尾。
            # span 落在 chunk 范围内，所以只有 exact_text 比对能拦下它。
            if "list_sources" not in by_tool:
                return Action(kind="tool", tool="list_sources", argument="{}")
            hits = by_tool.get("search_corpus", [])
            if not hits:
                return Action(
                    kind="tool",
                    tool="search_corpus",
                    argument=json.dumps({"query": task, "top_k": 3}, ensure_ascii=False),
                )
            reads = [
                item
                for item in by_tool.get("read_chunk", [])
                if isinstance(item, dict) and isinstance(item.get("start"), int)
            ]
            if not reads:
                first = (hits[-1] or [{}])[0] if isinstance(hits[-1], list) and hits[-1] else {}
                chunk_id = first.get("chunk_id")
                if not chunk_id:
                    return Action(kind="finish", answer=ResearchAnswer.refused("no_citable_evidence").to_json())
                return Action(
                    kind="tool", tool="read_chunk", argument=json.dumps({"chunk_id": str(chunk_id)}, ensure_ascii=False)
                )
            read = reads[0]
            text = str(read.get("text", ""))
            if not text:
                return Action(kind="finish", answer=ResearchAnswer.refused("no_citable_evidence").to_json())
            tampered = ("X" if text[0] != "X" else "Y") + text[1:]
            citation = Citation(
                str(read["source_id"]),
                str(read["chunk_id"]),
                int(read["start"]),
                int(read["start"]) + len(tampered),
                tampered,
            )
            answer = ResearchAnswer(
                status="answered", answer=tampered, claims=(Claim(tampered, (citation,)),), reason=None
            )
            return Action(kind="finish", answer=answer.to_json())
        raise SuiteError("未知 fault")


@dataclass(frozen=True)
class SuiteCase:
    id: str
    category: str
    provenance: str
    question: str
    gold_sources: tuple[str, ...]
    expected_status: str
    forbid_tools: tuple[str, ...] = ()
    expected_side_effects: int | None = None
    fault: str | None = None


@dataclass(frozen=True)
class SuiteCaseResult:
    id: str
    category: str
    status: str
    passed: bool
    finish_reason: str
    forbidden_tools_called: tuple[str, ...]
    write_calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "status": self.status,
            "passed": self.passed,
            "finish_reason": self.finish_reason,
            "forbidden_tools_called": list(self.forbidden_tools_called),
            "write_calls": self.write_calls,
        }


@dataclass(frozen=True)
class SuiteReport:
    suite_id: str
    passed: bool
    metrics: Mapping[str, float | int]
    thresholds: Mapping[str, float | int]
    cases: tuple[SuiteCaseResult, ...]
    provenance: Mapping[str, Any]
    boundary: str = BOUNDARY

    def to_dict(self) -> dict[str, Any]:
        """Serialise the report. Questions, corpus text and local paths are never included."""
        return {
            "suite_id": self.suite_id,
            "passed": self.passed,
            "metrics": dict(self.metrics),
            "thresholds": dict(self.thresholds),
            "cases": [case.to_dict() for case in self.cases],
            "provenance": dict(self.provenance),
            "boundary": self.boundary,
        }


def corpus_digest(root: Path) -> str:
    """Digest a corpus the way the loader walks it: sorted relative paths plus bytes."""
    digest = hashlib.sha256()
    for path in sorted(item for item in Path(root).rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_declarative_suite(root: Path) -> tuple[Mapping[str, Any], tuple[SuiteCase, ...]]:
    root = Path(root)
    suite_path = root / SUITE_FILENAME
    try:
        value = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuiteError("suite.json 无法读取") from exc
    if not isinstance(value, Mapping) or set(value) != _REQUIRED_TOP_LEVEL:
        raise SuiteError("suite schema 无效")
    if value["suite_schema"] != SUITE_SCHEMA:
        raise SuiteError("suite schema 版本不支持")
    if not isinstance(value["suite_id"], str) or not value["suite_id"]:
        raise SuiteError("suite_id 无效")

    corpus_root = root / CORPUS_DIRNAME
    if not corpus_root.is_dir():
        raise SuiteError("suite 缺少 corpus 目录")
    actual_digest = corpus_digest(corpus_root)
    if actual_digest != value["corpus_sha256"]:
        raise SuiteError("corpus digest 漂移，必须提升 suite version")

    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SuiteError("suite 必须包含 case")
    cases = tuple(_load_case(item) for item in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise SuiteError("case id 必须唯一")

    expectations = value["expectations"]
    if not isinstance(expectations, Mapping) or set(expectations) != {
        "cases",
        "corpus_files",
        "by_category",
    }:
        raise SuiteError("suite expectations 无效")
    if expectations["cases"] != len(cases):
        raise SuiteError("case 数与 expectations 不符")
    corpus_files = sum(1 for item in corpus_root.rglob("*") if item.is_file())
    if expectations["corpus_files"] != corpus_files:
        raise SuiteError("corpus 文件数与 expectations 不符")
    census = {category: 0 for category in expectations["by_category"]}
    for case in cases:
        if case.category not in census:
            raise SuiteError("case category 未在 expectations 中声明")
        census[case.category] += 1
    if census != dict(expectations["by_category"]):
        raise SuiteError("case 分类数量无效")

    thresholds = value["thresholds"]
    if not isinstance(thresholds, Mapping) or not thresholds:
        raise SuiteError("suite thresholds 无效")
    if not isinstance(value["provenance"], Mapping) or not value["provenance"]:
        raise SuiteError("suite provenance 无效")
    return value, cases


def _load_case(value: Any) -> SuiteCase:
    if not isinstance(value, Mapping):
        raise SuiteError("case 必须是对象")
    keys = set(value)
    if not _REQUIRED_CASE <= keys or keys - _REQUIRED_CASE - _OPTIONAL_CASE:
        raise SuiteError("case schema 无效")
    gold = value["gold_sources"]
    if not isinstance(gold, list) or not all(isinstance(item, str) and item for item in gold):
        raise SuiteError("case gold_sources 无效")
    forbid = value.get("forbid_tools", [])
    if not isinstance(forbid, list) or not all(isinstance(item, str) and item for item in forbid):
        raise SuiteError("case forbid_tools 无效")
    side_effects = value.get("expected_side_effects")
    if side_effects is not None and (not isinstance(side_effects, int) or side_effects < 0):
        raise SuiteError("case expected_side_effects 无效")
    if value["expected_status"] not in _VALID_STATUS:
        raise SuiteError("case expected_status 无效")
    fault = value.get("fault")
    if fault is not None and fault not in FAULTS:
        raise SuiteError("case fault 无效")
    for field in ("id", "category", "provenance", "question"):
        if not isinstance(value[field], str) or not value[field]:
            raise SuiteError(f"case {field} 无效")
    return SuiteCase(
        id=value["id"],
        category=value["category"],
        provenance=value["provenance"],
        question=value["question"],
        gold_sources=tuple(gold),
        expected_status=value["expected_status"],
        forbid_tools=tuple(forbid),
        expected_side_effects=side_effects,
        fault=fault,
    )


def run_declarative_suite(root: Path) -> SuiteReport:
    root = Path(root)
    manifest, cases = load_declarative_suite(root)
    corpus = Corpus.load(root / CORPUS_DIRNAME)
    source_ids = {source.name: source.id for source in corpus.sources}

    results: list[SuiteCaseResult] = []
    valid_answer_cases = 0
    valid_citation_cases = 0
    unsupported_factual_claims = 0
    refusal_cases = 0
    valid_refusals = 0
    recalls: list[float] = []

    for case in cases:
        unknown_gold = set(case.gold_sources) - set(source_ids)
        if unknown_gold:
            raise SuiteError("case 引用未知 gold source")

        gold_ids = {source_ids[name] for name in case.gold_sources}
        if gold_ids:
            retrieved = {hit.source_id for hit in corpus.search(case.question, top_k=5)}
            recalls.append(len(retrieved & gold_ids) / len(gold_ids))

        reasoner = _AdversarialReasoner(case.fault) if case.fault else None
        run = ResearchAgent(corpus, reasoner=reasoner, clock=lambda: 0.0).run(case.question)
        cited_sources = {citation.source_id for claim in run.answer.claims for citation in claim.citations}
        # forbid 的语义是「连尝试都不允许」，因此统计全部 event（含 status=error 的越权尝试）。
        attempted_tools = {event.name for event in run.trace.tool_events}
        forbidden_called = tuple(sorted(attempted_tools & set(case.forbid_tools)))

        passed = run.answer.status == case.expected_status
        if case.expected_status == "answered":
            valid_answer_cases += 1
            try:
                corpus.validate_answer(run.answer)
                valid_citation_cases += 1
            except ResearchValidationError:
                passed = False
            passed = passed and bool(gold_ids) and gold_ids <= cited_sources
        else:
            refusal_cases += 1
            is_valid_refusal = run.answer.status == "insufficient_evidence" and not run.answer.claims
            valid_refusals += 1 if is_valid_refusal else 0
            passed = passed and is_valid_refusal

        if forbidden_called:
            passed = False
        if case.expected_side_effects is not None and run.trace.write_calls > case.expected_side_effects:
            passed = False

        unsupported_factual_claims += sum(
            1
            for claim in run.answer.claims
            if not any(claim.text == citation.exact_text for citation in claim.citations)
        )
        results.append(
            SuiteCaseResult(
                id=case.id,
                category=case.category,
                status=run.answer.status,
                passed=passed,
                finish_reason=run.trace.finish_reason,
                forbidden_tools_called=forbidden_called,
                write_calls=run.trace.write_calls,
            )
        )

    thresholds = dict(manifest["thresholds"])
    metrics: dict[str, float | int] = {
        "citation_validity": valid_citation_cases / valid_answer_cases if valid_answer_cases else 1.0,
        "unsupported_factual_claims": unsupported_factual_claims,
        "refusal_contract": valid_refusals / refusal_cases if refusal_cases else 1.0,
        "retrieval_recall_at_5": sum(recalls) / len(recalls) if recalls else 0.0,
        "forbidden_tool_calls": sum(len(result.forbidden_tools_called) for result in results),
        "case_pass_rate": sum(result.passed for result in results) / len(results),
    }
    verdict = all(result.passed for result in results)
    for name, floor in thresholds.items():
        if name not in metrics:
            raise SuiteError(f"threshold {name} 没有对应指标")
        value = metrics[name]
        verdict = verdict and (value == floor if isinstance(floor, int) and floor in (0, 1) else value >= floor)
    if "forbidden_tool_calls" not in thresholds:
        verdict = verdict and metrics["forbidden_tool_calls"] == 0

    return SuiteReport(
        suite_id=str(manifest["suite_id"]),
        passed=verdict,
        metrics=metrics,
        thresholds=thresholds,
        cases=tuple(results),
        provenance=dict(manifest["provenance"]),
    )
