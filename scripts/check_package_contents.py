"""Fail closed when release archives contain generated or private material."""

from __future__ import annotations

import sys
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


BANNED_PARTS = frozenset({".env", ".git", "__pycache__", "build", "dist"})
BANNED_SUFFIXES = (".pyc", ".pyo")
BANNED_CONTENT = (
    b"/" + b"Users/",
    b"/" + b"home/",
    b"C:\\Users\\",
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
)

SDIST_ALLOWED = frozenset(
    """.dockerignore
.github/workflows/ci.yml
.gitignore
AGENTS.md
CONTRIBUTING.md
Dockerfile
LICENSE
PKG-INFO
README.md
SECURITY.md
docs/demo-output.txt
docs/evidence-research-agent-case-study.md
docs/environment-design.md
docs/evaluation-design.md
docs/fault-injection-experiments.md
docs/isolation-attack-matrix.md
docs/comparison-with-community-solutions.md
docs/known-gaps.md
docs/longterm-design.md
docs/memory-design.md
docs/ragchecker-paper-to-code-report.md
docs/rag-evaluation-report.md
docs/real-model-eval-report.md
docs/reasoning-design.md
docs/resilience-design.md
docs/retrieval-design.md
docs/retrieval-report.md
docs/public-release-readiness.md
docs/v0.9-v1.1-research-agent-plan.md
docs/v1.2-hybrid-rag-plan.md
docs/v1.3-ragchecker-paper-to-code-plan.md
docs/v1.5-controlled-memory-plan.md
evals/risk-v1/suite.json
evals/risk-v1/canonical-report.json
evals/risk-v1/PROVENANCE.md
evals/risk-v1/corpus/alpha-config.md
evals/risk-v1/corpus/beta-config.md
evals/risk-v1/corpus/catalog.md
evals/risk-v1/corpus/injection-field-note.md
evals/risk-v1/corpus/injection-handbook.md
evals/risk-v1/corpus/ops-runbook.md
evals/external-v1/suite.json
evals/external-v1/canonical-report.json
evals/external-v1/corpus/ccc-00.md
evals/external-v1/corpus/ccc-01.md
evals/external-v1/corpus/ccc-02.md
evals/external-v1/corpus/ccc-03.md
evals/external-v1/corpus/ccc-04.md
evals/external-v1/corpus/ccc-05.md
evals/external-v1/corpus/ccc-06.md
evals/external-v1/corpus/ccc-07.md
evals/external-v1/corpus/ccc-08.md
evals/external-v1/corpus/ccc-09.md
evals/external-v1/corpus/ccc-10.md
evals/external-v1/corpus/ccc-11.md
evals/external-v1/corpus/ccc-12.md
evals/external-v1/corpus/ccc-13.md
evals/external-v1/corpus/ccc-14.md
evals/external-v1/corpus/ccc-15.md
evals/external-v1/corpus/ccc-16.md
evals/external-v1/corpus/ccc-17.md
evals/external-v1/corpus/ccc-18.md
evals/external-v1/corpus/ccc-19.md
evals/external-v1/corpus/ccc-20.md
evals/external-v1/corpus/ccc-21.md
evals/external-v1/corpus/ccc-22.md
evals/external-v1/corpus/ccc-23.md
evals/external-v1/corpus/ccc-24.md
evals/external-v1/corpus/ccc-25.md
evals/external-v1/corpus/ccc-26.md
evals/external-v1/corpus/ccc-27.md
evals/external-v1/corpus/ccc-28.md
evals/external-v1/corpus/ccc-29.md
evals/controlled-memory-v1/baseline-report.json
evals/controlled-memory-v1/baseline-report.md
evals/controlled-memory-v1/cases.json
evals/controlled-memory-v1/records.json
evals/controlled-memory-v1/thresholds.json
evals/paper-ragchecker-v1/corpus/atlas-monthly.md
evals/paper-ragchecker-v1/corpus/atlas-quarterly.md
evals/paper-ragchecker-v1/corpus/harbor-noise.md
evals/paper-ragchecker-v1/canonical-report.json
evals/paper-ragchecker-v1/suite-v1.json
evals/research-agent/corpus/public-environment-v0.8.md
evals/research-agent/corpus/public-evaluation.md
evals/research-agent/corpus/public-live-eval.md
evals/research-agent/corpus/public-longterm-v0.8.md
evals/research-agent/corpus/public-memory.md
evals/research-agent/corpus/public-reasoning.md
evals/research-agent/corpus/public-research-v0.9.md
evals/research-agent/corpus/public-resilience.md
evals/research-agent/corpus/public-retrieval.md
evals/research-agent/corpus/synthetic-conflict-a.md
evals/research-agent/corpus/synthetic-conflict-b.md
evals/research-agent/corpus/synthetic-injection-a.md
evals/research-agent/corpus/synthetic-injection-b.md
evals/research-agent/corpus/synthetic-injection-c.md
evals/research-agent/suite-v1.json
evals/research-rag-v2/canonical-report.json
evals/research-rag-v2/corpus/automobile.md
evals/research-rag-v2/corpus/citation.md
evals/research-rag-v2/corpus/conflict-alpha.md
evals/research-rag-v2/corpus/conflict-beta.md
evals/research-rag-v2/corpus/distractor-astronomy.md
evals/research-rag-v2/corpus/distractor-cooking.md
evals/research-rag-v2/corpus/distractor-finance.md
evals/research-rag-v2/corpus/distractor-gardening.md
evals/research-rag-v2/corpus/distractor-weather.md
evals/research-rag-v2/corpus/injection.md
evals/research-rag-v2/corpus/memory.md
evals/research-rag-v2/corpus/semantic-zh.md
evals/research-rag-v2/suite-v2.json
example/conversation.json
example/corpus/agent-lab.md
pyproject.toml
scripts/import-external-suite.py
scripts/check_package_contents.py
scripts/evaluate_controlled_memory.py
scripts/evaluate_ragchecker_replication.py
src/agentlab/__init__.py
src/agentlab/api.py
src/agentlab/cli.py
src/agentlab/controlled_memory_evaluation.py
src/agentlab/demo.py
src/agentlab/environment.py
src/agentlab/evaluation/__init__.py
src/agentlab/evaluation/case.py
src/agentlab/evaluation/metrics.py
src/agentlab/evaluation/observability.py
src/agentlab/evaluation/real.py
src/agentlab/evaluation/regression.py
src/agentlab/evaluation/runner.py
src/agentlab/llm.py
src/agentlab/longterm.py
src/agentlab/memory.py
src/agentlab/memory_tools.py
src/agentlab/messages.py
src/agentlab/reasoning.py
src/agentlab/research/__init__.py
src/agentlab/research/agent.py
src/agentlab/research/artifacts.py
src/agentlab/research/corpus.py
src/agentlab/research/evaluation.py
src/agentlab/research/fixtures/demo-corpus/architecture.md
src/agentlab/research/fixtures/demo-corpus/security.md
src/agentlab/research/live.py
src/agentlab/research/prompt_profiles.py
src/agentlab/research/rag_evaluation.py
src/agentlab/research/ragchecker_evaluation.py
src/agentlab/research/ragchecker_metrics.py
src/agentlab/research/reasoner.py
src/agentlab/research/retrieval.py
src/agentlab/research/suite_runner.py
src/agentlab/research/tools.py
src/agentlab/research/vector_index.py
src/agentlab/resilience/__init__.py
src/agentlab/resilience/cache.py
src/agentlab/resilience/circuit.py
src/agentlab/resilience/loadtest.py
src/agentlab/resilience/queue.py
src/agentlab/retrieval/__init__.py
src/agentlab/retrieval/chunking.py
src/agentlab/retrieval/evaluate.py
src/agentlab/retrieval/experiment.py
src/agentlab/retrieval/index.py
src/agentlab/summarize.py
tests/test_api.py
tests/test_cli.py
tests/test_controlled_memory_evaluation.py
tests/test_environment.py
tests/test_evaluation.py
tests/test_llm.py
tests/test_longterm.py
tests/test_memory.py
tests/test_memory_tools.py
tests/test_package_contents.py
tests/test_reasoning.py
tests/test_research_agent.py
tests/test_research_artifacts.py
tests/test_research_corpus.py
tests/test_research_suite_runner.py
tests/test_research_evaluation.py
tests/test_research_live.py
tests/test_research_rag_evaluation.py
tests/test_research_ragchecker_evaluation.py
tests/test_research_ragchecker_metrics.py
tests/test_research_retrieval.py
tests/test_research_tools.py
tests/test_research_vector_index.py
tests/test_resilience.py
tests/test_retrieval.py""".splitlines()
)

WHEEL_CODE_ALLOWED = frozenset(
    name.removeprefix("src/")
    for name in SDIST_ALLOWED
    if name.startswith("src/agentlab/")
)
DIST_INFO_FILES = frozenset(
    {"METADATA", "WHEEL", "entry_points.txt", "RECORD", "licenses/LICENSE"}
)


def _members(path: Path) -> Iterable[tuple[str, bytes, bool]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                mode = info.external_attr >> 16
                regular = not info.is_dir() and not stat.S_ISLNK(mode)
                yield info.filename, archive.read(info) if regular else b"", regular
        return
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            content = b""
            if member.isfile():
                handle = archive.extractfile(member)
                if handle is not None:
                    content = handle.read()
            yield member.name, content, member.isfile()


def audit(path: Path) -> list[str]:
    failures: list[str] = []
    names: set[str] = set()
    is_wheel = path.suffix == ".whl"
    for archive_name, content, regular in _members(path):
        name = archive_name
        if not is_wheel:
            parts = PurePosixPath(archive_name).parts
            if len(parts) < 2 or not parts[0].startswith("agent_lab-"):
                failures.append(f"invalid sdist root: {archive_name}")
                continue
            name = PurePosixPath(*parts[1:]).as_posix()
        normalized = PurePosixPath(name)
        if name in names:
            failures.append(f"duplicate archive member: {name}")
        names.add(name)
        if not regular:
            failures.append(f"non-regular archive member: {name}")
        if any(part in BANNED_PARTS or part.endswith(".egg-info") for part in normalized.parts):
            failures.append(f"generated/private path: {name}")
        if name.endswith(BANNED_SUFFIXES):
            failures.append(f"compiled file: {name}")
        if any(marker in content for marker in BANNED_CONTENT):
            failures.append(f"sensitive content marker in: {name}")
    expected = _expected_wheel_members(names) if is_wheel else SDIST_ALLOWED
    failures.extend(f"unexpected archive member: {name}" for name in sorted(names - expected))
    failures.extend(f"missing archive member: {name}" for name in sorted(expected - names))
    return failures


def _expected_wheel_members(names: set[str]) -> frozenset[str]:
    dist_dirs = {
        PurePosixPath(name).parts[0]
        for name in names
        if PurePosixPath(name).parts and PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    if len(dist_dirs) != 1:
        return WHEEL_CODE_ALLOWED
    dist_dir = next(iter(dist_dirs))
    return WHEEL_CODE_ALLOWED | {f"{dist_dir}/{name}" for name in DIST_INFO_FILES}


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("dist")
    archives = sorted((*root.glob("*.whl"), *root.glob("*.tar.gz")))
    if len(archives) != 2:
        print("package audit expected one wheel and one sdist", file=sys.stderr)
        return 2
    failures = [failure for archive in archives for failure in audit(archive)]
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("package audit passed: 2 archives")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
