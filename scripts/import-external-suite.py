"""Import the external-provenance evaluation suite from SQuAD 2.0.

Why this script exists
----------------------
``evals/research-agent/`` is fully self-authored: the corpus is Agent Lab's own
public design docs and the questions were written by the maintainer. A reviewer
is therefore entitled to ask "where did the evaluation samples come from, and
who wrote them?" with no external answer available.

This script builds ``evals/external-v1/`` from an **externally authored** source:
the SQuAD 2.0 dev set (crowd-written questions, crowd-annotated answer spans and
crowd-labelled unanswerable questions). Both the corpus text and every question
originate outside this project.

Properties required by AGENTS.md
--------------------------------
* provenance is recorded per suite: dataset name, version, URL, license, fetch
  time, and the SHA-256 of the downloaded artefact;
* the fetch is reproducible offline via ``--check`` against the recorded digests;
* no private files, restricted material, real user data, or login-gated content;
* the corpus stays inside the loader limits (``MAX_SOURCES`` 32 files,
  1 MiB per file, 8 MiB total).

Usage
-----
    python scripts/import-external-suite.py            # fetch + rebuild
    python scripts/import-external-suite.py --check    # verify, no network

The script never writes outside ``evals/external-v1/`` and never writes to the
network in ``--check`` mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_DIR = REPO_ROOT / "evals" / "external-v1"
CORPUS_DIR = SUITE_DIR / "corpus"
CACHE_PATH = SUITE_DIR / ".source-cache.json"

DATASET = {
    "name": "SQuAD 2.0 (Stanford Question Answering Dataset, dev set)",
    "version": "v2.0",
    "url": "https://rajpurkar.github.io/SQuAD-explorer/dataset/dev-v2.0.json",
    "mirror": "https://raw.githubusercontent.com/rajpurkar/SQuAD-explorer/master/dataset/dev-v2.0.json",
    "license": "CC BY-SA 4.0",
    "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "citation": (
        "Rajpurkar, Jia, Liang. Know What You Don't Know: Unanswerable Questions "
        "for SQuAD. ACL 2018."
    ),
    "sha256": "80a5225e94905956a6446d296ca1093975c4d3b3260f1d6c8f68bc2ab77182d8",
    "bytes": 4_181_095,
}

# One article keeps the corpus topically coherent, which is what makes retrieval
# non-trivial: every paragraph shares vocabulary with every other paragraph.
SOURCE_ARTICLE = "Computational_complexity_theory"
MIN_PARAGRAPH_CHARS = 400
MAX_PARAGRAPHS = 30
ANSWERABLE_PER_PARAGRAPH = 2
UNANSWERABLE_PER_PARAGRAPH = 1

SUITE_ID = "external-v1"
SUITE_SCHEMA = 1


class ImportError_(RuntimeError):
    pass


def _normalize(text: str) -> str:
    """Mirror agentlab.research.corpus._normalize so offsets stay valid."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _slug(name: str) -> str:
    keep = [char if char.isalnum() else "-" for char in name.lower()]
    return "".join(keep).strip("-")[:48] or "source"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed https URL
        return response.read()


def _load_source() -> dict:
    if CACHE_PATH.exists():
        raw = CACHE_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() == DATASET["sha256"]:
            return json.loads(raw.decode("utf-8"))
    raw = _fetch(DATASET["url"])
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DATASET["sha256"]:
        raise ImportError_(f"数据集 digest 不匹配: {digest} != {DATASET['sha256']}")
    if len(raw) != DATASET["bytes"]:
        raise ImportError_(f"数据集大小不匹配: {len(raw)} != {DATASET['bytes']}")
    return json.loads(raw.decode("utf-8"))


def _select(article: dict) -> list[dict]:
    paragraphs = [
        para
        for para in article["paragraphs"]
        if len(_normalize(para["context"])) >= MIN_PARAGRAPH_CHARS
    ]
    # deterministic: sort by title-free content digest, then take the first N
    paragraphs.sort(key=lambda para: hashlib.sha256(_normalize(para["context"]).encode()).hexdigest())
    return paragraphs[:MAX_PARAGRAPHS]


def _build_cases(paragraphs: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    cases: list[dict] = []
    written: list[tuple[str, str]] = []
    for index, para in enumerate(paragraphs):
        filename = f"ccc-{index:02d}.md"
        text = _normalize(para["context"])
        answerable = [qa for qa in para["qas"] if not qa.get("is_impossible")]
        unanswerable = [qa for qa in para["qas"] if qa.get("is_impossible")]
        # deterministic pick: sort by question text
        answerable.sort(key=lambda qa: qa["question"])
        unanswerable.sort(key=lambda qa: qa["question"])

        for qa in answerable[:ANSWERABLE_PER_PARAGRAPH]:
            answers = qa.get("answers") or []
            if not answers:
                continue
            best = min(answers, key=lambda item: item["answer_start"])
            start = best["answer_start"]
            end = start + len(best["text"])
            # Offsets are only meaningful if they still address the normalised text.
            if text[start:end] != best["text"]:
                raise ImportError_(f"答案偏移在规范化后失效: {qa['id']}")
            cases.append(
                {
                    "id": f"{filename}-{qa['id']}",
                    # 不按序号人为切分「多跳」：SQuAD 2.0 没有多跳标注，
                    # 造一个假的 category 会让报告看起来比实际更强。
                    "category": "external_direct",
                    "provenance": "external",
                    "question": qa["question"],
                    "gold_sources": [filename],
                    "expected_status": "answered",
                    "gold_span": {"start": start, "end": end, "exact_text": best["text"]},
                    "source_qa_id": qa["id"],
                }
            )
            written.append((qa["id"], filename))

        for qa in unanswerable[:UNANSWERABLE_PER_PARAGRAPH]:
            cases.append(
                {
                    "id": f"{filename}-{qa['id']}",
                    "category": "external_unanswerable",
                    "provenance": "external",
                    "question": qa["question"],
                    "gold_sources": [],
                    "expected_status": "insufficient_evidence",
                    "source_qa_id": qa["id"],
                }
            )
            written.append((qa["id"], filename))
    return cases, written


def _corpus_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build() -> dict:
    source = _load_source()
    article = next((item for item in source["data"] if item["title"] == SOURCE_ARTICLE), None)
    if article is None:
        raise ImportError_(f"数据集里找不到文章 {SOURCE_ARTICLE}")

    paragraphs = _select(article)
    if not paragraphs:
        raise ImportError_("没有满足长度门槛的段落")
    if len(paragraphs) > 32:
        raise ImportError_("段落数超过 corpus 加载上限 32")

    cases, written = _build_cases(paragraphs)

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in CORPUS_DIR.glob("*.md"):
        stale.unlink()
    for index, para in enumerate(paragraphs):
        (CORPUS_DIR / f"ccc-{index:02d}.md").write_text(_normalize(para["context"]), encoding="utf-8")

    digest = _corpus_digest(CORPUS_DIR)
    suite = {
        "suite_id": SUITE_ID,
        "suite_schema": SUITE_SCHEMA,
        "corpus_sha256": digest,
        "provenance": {
            **{key: DATASET[key] for key in ("name", "version", "url", "license", "license_url", "citation", "sha256")},
            "article": SOURCE_ARTICLE,
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "selection_rule": (
                f"paragraphs >= {MIN_PARAGRAPH_CHARS} chars, sorted by SHA-256 of normalised text, "
                f"first {MAX_PARAGRAPHS}; per paragraph up to {ANSWERABLE_PER_PARAGRAPH} answerable and "
                f"{UNANSWERABLE_PER_PARAGRAPH} unanswerable questions, sorted by question text"
            ),
            "transformations": [
                "CRLF/CR normalised to LF (matches agentlab.research.corpus._normalize)",
                "answer offsets recomputed and asserted against the normalised text",
                "one corpus file per paragraph, named ccc-<index>.md",
            ],
        },
        "expectations": {
            "cases": len(cases),
            "corpus_files": len(paragraphs),
            "by_category": {
                category: sum(1 for case in cases if case["category"] == category)
                for category in sorted({case["category"] for case in cases})
            },
        },
        "thresholds": {
            "citation_validity": 1.0,
            "unsupported_factual_claims": 0,
            "refusal_contract": 1.0,
            "retrieval_recall_at_5": 0.90,
        },
        "cases": cases,
    }
    (SUITE_DIR / "suite.json").write_text(
        json.dumps(suite, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"corpus files : {len(paragraphs)}")
    print(f"cases        : {len(cases)}  {suite['expectations']['by_category']}")
    print(f"corpus sha256: {digest}")
    return suite


def check() -> int:
    suite_path = SUITE_DIR / "suite.json"
    if not suite_path.exists():
        print("suite.json 不存在", file=sys.stderr)
        return 1
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    digest = _corpus_digest(CORPUS_DIR)
    failures: list[str] = []
    if digest != suite["corpus_sha256"]:
        failures.append(f"corpus digest 漂移: {digest} != {suite['corpus_sha256']}")
    files = sorted(path.name for path in CORPUS_DIR.glob("*.md"))
    if len(files) != suite["expectations"]["corpus_files"]:
        failures.append(f"corpus 文件数不符: {len(files)}")
    if len(suite["cases"]) != suite["expectations"]["cases"]:
        failures.append("case 数与 expectations 不符")
    if suite["provenance"]["license"] not in {"CC BY-SA 4.0", "CC BY 4.0", "CC0 1.0", "public domain"}:
        failures.append("许可字段不是允许的值")
    for case in suite["cases"]:
        if case["expected_status"] not in {"answered", "insufficient_evidence"}:
            failures.append(f"case 状态非法: {case['id']}")
            break
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"check ok: {len(files)} corpus files, {len(suite['cases'])} cases, digest {digest[:16]}…")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify existing artifacts offline")
    args = parser.parse_args()
    if args.check:
        return check()
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
