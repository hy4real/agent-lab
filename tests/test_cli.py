"""CLI output and authorization boundary tests."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import agentlab.cli as cli
from agentlab.cli import main
from agentlab.research.corpus import Corpus
from agentlab.research.tasks import ResearchTaskStore


def test_research_demo_is_byte_deterministic(capsys) -> None:
    assert main(["research-demo"]) == 0
    first = capsys.readouterr()
    assert main(["research-demo"]) == 0
    second = capsys.readouterr()
    assert first.out == second.out
    assert first.err == second.err == ""
    answer = json.loads(first.out)["answer"]
    assert answer["status"] == "answered"
    assert len(answer["claims"]) == 1
    assert "checks every citation" in answer["answer"]


def test_worker_cli_processes_saved_request_and_reports_idle(tmp_path: Path, capsys) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "fact.md").write_text("The Helix fixture status is ready.", encoding="utf-8")
    path = tmp_path / "state" / "tasks.sqlite3"
    store = ResearchTaskStore(path)
    task, _ = store.submit("req", "What is the Helix fixture status?", Corpus.load(root).content_hash)
    args = ["research-worker", "--corpus", str(root), "--task-db", str(path), "--once"]
    assert main(args) == 0
    output = capsys.readouterr()
    events = [json.loads(line) for line in output.err.splitlines()]
    assert [event["event"] for event in events] == ["worker_claimed", "worker_finished"]
    assert all(event["task_id"] == task.id for event in events)
    assert json.loads(output.out) == {"task_id": task.id, "status": "succeeded", "attempt": 1}
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert main(args + ["--lease-seconds", "0"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err == "error: agentlab_command_failed\n"


def test_worker_cli_failed_task_and_interrupt(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "fact.md").write_text("Synthetic corpus.", encoding="utf-8")
    path = tmp_path / "state" / "tasks.sqlite3"
    ResearchTaskStore(path).submit("req", "question", "a" * 64)
    args = ["research-worker", "--corpus", str(root), "--task-db", str(path)]
    assert main(args + ["--once"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    assert main(args) == 130
    assert capsys.readouterr().out == ""


def test_research_defaults_to_offline(tmp_path: Path, capsys) -> None:
    (tmp_path / "fact.md").write_text("The Helix fixture status is ready.", encoding="utf-8")
    assert main(["research", "--corpus", str(tmp_path), "--question", "What is the Helix fixture status?"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["answer"]["status"] == "answered"


def test_cli_errors_are_stable_and_do_not_echo_path(tmp_path: Path, capsys) -> None:
    secret_path = tmp_path / "private-name"
    assert main(["research", "--corpus", str(secret_path), "--question", "q"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: agentlab_command_failed\n"
    assert str(secret_path) not in captured.err


def test_research_eval_stdout_is_json(capsys) -> None:
    root = Path(__file__).parents[1] / "evals" / "research-agent"
    assert main(["research-eval", "--suite-root", str(root)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["passed"] is True


def test_claim_suite_validation_is_redacted_json(tmp_path: Path, monkeypatch, capsys) -> None:
    report = SimpleNamespace(
        to_dict=lambda: {
            "suite_id": "private-suite",
            "case_count": 24,
            "frozen_ready": True,
        }
    )
    monkeypatch.setattr(cli, "validate_local_claim_suite", lambda root: report)
    private_root = tmp_path / "private-marker"
    assert main(["claim-suite-validate", "--suite-root", str(private_root)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["frozen_ready"] is True
    assert str(private_root) not in captured.out


def test_claim_suite_validation_error_is_stable(tmp_path: Path, capsys) -> None:
    private_root = tmp_path / "private-marker"
    assert main(["claim-suite-validate", "--suite-root", str(private_root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: agentlab_command_failed\n"
    assert str(private_root) not in captured.err


def test_memory_cli_lifecycle_is_explicit_and_redacted(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = tmp_path / "private-state.json"
    secret = "private memory body"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
    assert main([
        "memory",
        "propose",
        "--state",
        str(state),
        "--scope",
        "project:alpha",
        "--kind",
        "decision",
        "--origin",
        "manual",
        "--expected-revision",
        "0",
    ]) == 0
    proposed = json.loads(capsys.readouterr().out)
    assert proposed["record"]["status"] == "candidate"
    assert proposed["revision"] == 1
    assert secret not in json.dumps(proposed)

    record_id = proposed["record"]["id"]
    assert main([
        "memory",
        "confirm",
        record_id,
        "--state",
        str(state),
        "--scope",
        "project:alpha",
        "--expected-revision",
        "1",
    ]) == 0
    confirmed = json.loads(capsys.readouterr().out)
    assert confirmed["record"]["status"] == "confirmed"

    assert main([
        "memory",
        "list",
        "--state",
        str(state),
        "--scope",
        "project:alpha",
    ]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["revision"] == 2
    assert listed["records"][0]["status"] == "confirmed"
    assert secret not in json.dumps(listed)

    assert main([
        "memory",
        "deactivate",
        record_id,
        "--state",
        str(state),
        "--scope",
        "project:alpha",
        "--expected-revision",
        "2",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["record"]["status"] == "inactive"


def test_memory_cli_import_is_dry_run_by_default(tmp_path: Path, capsys) -> None:
    state = tmp_path / "legacy.json"
    original = json.dumps({
        "entries": [
            {"id": "entry-1", "text": "legacy text", "created_at": "2026-08-14T00:00:00+00:00"}
        ]
    }).encode()
    state.write_bytes(original)
    assert main([
        "memory",
        "import-legacy",
        "--state",
        str(state),
        "--scope",
        "project:alpha",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False
    assert state.read_bytes() == original


def test_memory_cli_errors_do_not_echo_state_or_stdin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = tmp_path / "private-state-name.json"
    secret = "private invalid body"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret))
    assert main([
        "memory",
        "propose",
        "--state",
        str(state),
        "--scope",
        "bad scope",
        "--kind",
        "note",
        "--origin",
        "manual",
        "--expected-revision",
        "0",
    ]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: agentlab_command_failed\n"
    assert str(state) not in captured.err
    assert secret not in captured.err


def test_research_index_build_uses_explicit_optional_backend(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "fact.md").write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(cli, "FastEmbedProvider", lambda **_kwargs: object())
    manifest = SimpleNamespace(to_dict=lambda: {"index_version": "a" * 64})
    monkeypatch.setattr(cli, "build_local_index", lambda *args, **kwargs: manifest)
    assert main([
        "research-index",
        "build",
        "--corpus",
        str(tmp_path / "corpus"),
        "--index-dir",
        str(tmp_path / "index"),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["index_version"] == "a" * 64

    monkeypatch.setattr(cli, "verify_local_index", lambda *args, **kwargs: manifest)
    assert main([
        "research-index",
        "verify",
        "--corpus",
        str(tmp_path / "corpus"),
        "--index-dir",
        str(tmp_path / "index"),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["index_version"] == "a" * 64


def test_research_rag_eval_uses_index_provenance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    closed = []
    corpus = object()
    manifest = SimpleNamespace(
        public_provenance=lambda: {"index_version": "b" * 64}
    )

    class FakeVectorSearch:
        index_version = "b" * 64

        def __init__(self, loaded_corpus, index_dir, embeddings):
            assert loaded_corpus is corpus
            assert index_dir == tmp_path / "index"
            assert embeddings == "embeddings"
            self.manifest = manifest

        def close(self):
            closed.append(True)

    report = SimpleNamespace(
        passed=True,
        to_dict=lambda: {"result": "passed", "index_version": "b" * 64},
    )
    monkeypatch.setattr(cli, "load_rag_fixture", lambda root: (("case",), corpus))
    monkeypatch.setattr(cli, "FastEmbedProvider", lambda **kwargs: "embeddings")
    monkeypatch.setattr(cli, "QdrantVectorSearch", FakeVectorSearch)
    monkeypatch.setattr(cli, "HybridRetriever", lambda loaded_corpus, vector: "hybrid")
    monkeypatch.setattr(
        cli,
        "run_rag_evaluation",
        lambda cases, loaded_corpus, hybrid, **kwargs: report,
    )

    assert main([
        "research-rag-eval",
        "--suite-root",
        str(tmp_path / "suite"),
        "--index-dir",
        str(tmp_path / "index"),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "passed"
    assert closed == [True]


def test_hybrid_cli_requires_index_and_records_mode(tmp_path: Path, capsys) -> None:
    (tmp_path / "fact.md").write_text("synthetic", encoding="utf-8")
    assert main([
        "research",
        "--corpus",
        str(tmp_path),
        "--question",
        "q",
        "--retrieval",
        "hybrid",
    ]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: agentlab_command_failed\n"


def test_hybrid_research_uses_local_index(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "fact.md").write_text("synthetic", encoding="utf-8")
    closed = []

    class FakeVectorSearch:
        index_version = "c" * 64

        def __init__(self, corpus, index_dir, embeddings):
            assert index_dir == tmp_path / "index"
            assert embeddings == "embeddings"

        def close(self):
            closed.append(True)

    class FakeAgent:
        def __init__(self, corpus, **kwargs):
            assert kwargs["search"] == "hybrid"
            assert kwargs["retrieval_mode"] == "hybrid"
            assert kwargs["index_version"] == "c" * 64
            assert kwargs["prompt_profile_id"] == "grounded-hybrid-v1"

        def run(self, question):
            return SimpleNamespace(to_dict=lambda: {"answer": {"status": "answered"}})

    monkeypatch.setattr(
        cli,
        "FastEmbedProvider",
        lambda **kwargs: "embeddings" if kwargs["cache_dir"] == tmp_path / "cache" else None,
    )
    monkeypatch.setattr(cli, "QdrantVectorSearch", FakeVectorSearch)
    monkeypatch.setattr(cli, "HybridRetriever", lambda corpus, vector: "hybrid")
    monkeypatch.setattr(cli, "ResearchAgent", FakeAgent)

    assert main([
        "research",
        "--corpus",
        str(tmp_path),
        "--question",
        "q",
        "--retrieval",
        "hybrid",
        "--index-dir",
        str(tmp_path / "index"),
        "--model-cache",
        str(tmp_path / "cache"),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["answer"]["status"] == "answered"
    assert closed == [True]


def test_live_mode_constructs_reasoner_and_warns(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "fact.md").write_text("The live fixture is synthetic.", encoding="utf-8")
    backend = SimpleNamespace(usage={})
    monkeypatch.setattr(cli, "LLMBackend", lambda **_kwargs: backend)

    class FakeAgent:
        def __init__(self, corpus, *, reasoner=None, **kwargs):
            assert reasoner is not None
            assert kwargs["retrieval_mode"] == "lexical"
            assert kwargs["prompt_profile_id"] == "baseline-v1"

        def run(self, question):
            return SimpleNamespace(to_dict=lambda: {"answer": {"status": "insufficient_evidence"}})

    monkeypatch.setattr(cli, "ResearchAgent", FakeAgent)
    assert main([
        "research", "--corpus", str(tmp_path), "--question", "synthetic question", "--live"
    ]) == 0
    captured = capsys.readouterr()
    assert "selected chunks" in captured.err
    assert json.loads(captured.out)["answer"]["status"] == "insufficient_evidence"


def test_live_provider_failure_is_stable(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "fact.md").write_text("synthetic", encoding="utf-8")

    def fail_backend(**_kwargs):
        raise ImportError("private provider diagnostic")

    monkeypatch.setattr(cli, "LLMBackend", fail_backend)
    assert main([
        "research", "--corpus", str(tmp_path), "--question", "q", "--live"
    ]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: agentlab_live_provider_failed\n" in captured.err
    assert "private provider diagnostic" not in captured.err


def test_serve_uses_explicit_loopback_or_container_host(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "fact.md").write_text("The server fixture is local.", encoding="utf-8")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, *, host, port, access_log: calls.append((app, host, port, access_log)))
    assert main(["serve", "--corpus", str(tmp_path), "--port", "8123"]) == 0
    assert calls[-1][1:] == ("127.0.0.1", 8123, False)
    assert main([
        "serve", "--corpus", str(tmp_path), "--port", "8124", "--bind", "container"
    ]) == 0
    assert calls[-1][1:] == ("0.0.0.0", 8124, False)
    assert capsys.readouterr().err.count("session token:") == 2


def test_serve_rejects_invalid_port(tmp_path: Path, capsys) -> None:
    assert main(["serve", "--corpus", str(tmp_path), "--port", "0"]) == 2
    assert capsys.readouterr().err == "error: agentlab_command_failed\n"
