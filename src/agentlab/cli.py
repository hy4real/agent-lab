"""Command-line interface with JSON stdout and diagnostics on stderr."""

from __future__ import annotations

import argparse
import json
import secrets
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Sequence

from .llm import LLMBackend
from .longterm import LongTermMemory, LongTermMemoryError, MemoryRecord
from .research.agent import ResearchAgent
from .research.corpus import Corpus, CorpusError
from .research.evaluation import EvaluationError, run_frozen_evaluation
from .research.suite_runner import run_declarative_suite
from .research.live import LiveResearchReasoner
from .research.local_claim_evaluation import (
    LocalClaimEvaluationError,
    validate_local_claim_suite,
)
from .research.prompt_profiles import (
    BASELINE_PROFILE_ID,
    GROUNDED_HYBRID_PROFILE_ID,
    prompt_profile_ids,
)
from .research.rag_evaluation import (
    RagEvaluationError,
    load_rag_fixture,
    run_rag_evaluation,
)
from .research.retrieval import HybridRetriever
from .research.tasks import ResearchTaskStore
from .research.worker import ResearchWorker
from .research.vector_index import (
    FastEmbedProvider,
    QdrantVectorSearch,
    RagIndexError,
    build_local_index,
    verify_local_index,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentlab", description="Local evidence research agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("research-demo", help="run the packaged synthetic offline demo")

    research = subparsers.add_parser("research", help="research an explicit local corpus")
    research.add_argument("--corpus", required=True, type=Path)
    research.add_argument("--question", required=True)
    research.add_argument("--live", action="store_true", help="allow an OpenAI-compatible Provider call")
    research.add_argument("--model", default="deepseek-chat")
    research.add_argument("--base-url")
    research.add_argument("--retrieval", choices=("lexical", "hybrid"), default="lexical")
    research.add_argument("--index-dir", type=Path)
    research.add_argument("--model-cache", type=Path)
    research.add_argument("--prompt-profile", choices=prompt_profile_ids())

    index = subparsers.add_parser("research-index", help="build or verify a local Qdrant index")
    index.add_argument("action", choices=("build", "verify", "rebuild"))
    index.add_argument("--corpus", required=True, type=Path)
    index.add_argument("--index-dir", required=True, type=Path)
    index.add_argument("--model-cache", type=Path)

    rag_evaluate = subparsers.add_parser(
        "research-rag-eval",
        help="run the frozen Hybrid RAG suite against a verified local index",
    )
    rag_evaluate.add_argument("--suite-root", required=True, type=Path)
    rag_evaluate.add_argument("--index-dir", required=True, type=Path)
    rag_evaluate.add_argument("--model-cache", type=Path)

    evaluate = subparsers.add_parser("research-eval", help="run a frozen offline research suite")
    evaluate.add_argument("--suite-root", required=True, type=Path)

    suite_eval = subparsers.add_parser(
        "research-suite-eval", help="run a declarative offline suite (suite.json driven)"
    )
    suite_eval.add_argument("--suite-root", required=True, type=Path)
    suite_eval.add_argument("--report", type=Path, default=None, help="optionally write the canonical report here")

    claim_suite = subparsers.add_parser(
        "claim-suite-validate",
        help="validate a private claim suite with explicit review state without Provider calls",
    )
    claim_suite.add_argument("--suite-root", required=True, type=Path)

    memory = subparsers.add_parser("memory", help="manage controlled local long-term memory")
    memory_actions = memory.add_subparsers(dest="memory_action", required=True)
    memory_propose = memory_actions.add_parser("propose", help="create an inactive memory candidate from stdin")
    _add_memory_state_arguments(memory_propose)
    memory_propose.add_argument("--scope", required=True)
    memory_propose.add_argument(
        "--kind",
        required=True,
        choices=("note", "preference", "decision", "constraint"),
    )
    memory_propose.add_argument("--origin", required=True, choices=("manual", "message"))
    memory_propose.add_argument("--source-message-id", action="append", default=[])
    memory_propose.add_argument("--expected-revision", required=True, type=int)

    for action, help_text in (
        ("confirm", "confirm one candidate"),
        ("deactivate", "deactivate one candidate or confirmed record"),
    ):
        transition = memory_actions.add_parser(action, help=help_text)
        _add_memory_state_arguments(transition)
        transition.add_argument("record_id")
        transition.add_argument("--scope", required=True)
        transition.add_argument("--expected-revision", required=True, type=int)

    memory_list = memory_actions.add_parser("list", help="list memory metadata without record text")
    _add_memory_state_arguments(memory_list)
    memory_list.add_argument("--scope", required=True)
    memory_list.add_argument("--status", choices=("candidate", "confirmed", "inactive"))

    memory_import = memory_actions.add_parser("import-legacy", help="dry-run or apply an in-place v1 import")
    _add_memory_state_arguments(memory_import)
    memory_import.add_argument("--scope", required=True)
    memory_import.add_argument("--apply", action="store_true")

    serve = subparsers.add_parser("serve", help="serve a fixed corpus over loopback")
    serve.add_argument("--corpus", required=True, type=Path)
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--bind", choices=("loopback", "container"), default="loopback")
    serve.add_argument("--task-db", type=Path, help="enable persistent task API using a private SQLite path")

    worker = subparsers.add_parser("research-worker", help="process persistent offline research tasks")
    worker.add_argument("--corpus", required=True, type=Path)
    worker.add_argument("--task-db", required=True, type=Path)
    worker.add_argument("--worker-id", default=None)
    worker.add_argument("--lease-seconds", type=float, default=30.0)
    worker.add_argument("--once", action="store_true", help="attempt at most one task and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "research-demo":
            root = Path(__file__).parent / "research" / "fixtures" / "demo-corpus"
            run = ResearchAgent(Corpus.load(root), clock=lambda: 0.0).run(
                "What does the final validator check for every citation?"
            )
            _write_json(run.to_dict())
            return 0
        if args.command == "research":
            corpus = Corpus.load(args.corpus)
            if args.retrieval == "lexical" and (
                args.index_dir is not None or args.model_cache is not None
            ):
                raise ValueError("lexical retrieval 不接受 index-dir 或 model-cache")
            if args.retrieval == "hybrid" and args.index_dir is None:
                raise ValueError("hybrid retrieval 需要 index-dir")
            profile_id = args.prompt_profile or (
                GROUNDED_HYBRID_PROFILE_ID
                if args.retrieval == "hybrid"
                else BASELINE_PROFILE_ID
            )
            reasoner = None
            if args.live:
                print(
                    "live boundary: sending the question, selected chunks, and tool observations to the configured Provider",
                    file=sys.stderr,
                )
                try:
                    backend = LLMBackend(base_url=args.base_url, model=args.model)
                    reasoner = LiveResearchReasoner(backend, prompt_profile_id=profile_id)
                except Exception:
                    print("error: agentlab_live_provider_failed", file=sys.stderr)
                    return 2
            vector_search = None
            try:
                search = None
                index_version = None
                if args.retrieval == "hybrid":
                    vector_search = QdrantVectorSearch(
                        corpus,
                        args.index_dir,
                        FastEmbedProvider(cache_dir=args.model_cache),
                    )
                    search = HybridRetriever(corpus, vector_search)
                    index_version = vector_search.index_version
                run = ResearchAgent(
                    corpus,
                    reasoner=reasoner,
                    search=search,
                    retrieval_mode=args.retrieval,
                    index_version=index_version,
                    prompt_profile_id=profile_id,
                ).run(args.question)
            finally:
                if vector_search is not None:
                    vector_search.close()
            _write_json(run.to_dict())
            return 0
        if args.command == "research-index":
            corpus = Corpus.load(args.corpus)
            embeddings = FastEmbedProvider(cache_dir=args.model_cache)
            if args.action == "verify":
                manifest = verify_local_index(corpus, args.index_dir, embeddings)
            else:
                manifest = build_local_index(
                    corpus,
                    args.index_dir,
                    embeddings,
                    rebuild=args.action == "rebuild",
                )
            _write_json(manifest.to_dict())
            return 0
        if args.command == "research-rag-eval":
            cases, corpus = load_rag_fixture(args.suite_root)
            vector_search = QdrantVectorSearch(
                corpus,
                args.index_dir,
                FastEmbedProvider(cache_dir=args.model_cache),
            )
            try:
                report = run_rag_evaluation(
                    cases,
                    corpus,
                    HybridRetriever(corpus, vector_search),
                    index_version=vector_search.index_version,
                    index_provenance=vector_search.manifest.public_provenance(),
                )
            finally:
                vector_search.close()
            _write_json(report.to_dict())
            return 0 if report.passed else 1
        if args.command == "research-eval":
            _write_json(run_frozen_evaluation(args.suite_root).to_dict())
            return 0
        if args.command == "research-suite-eval":
            suite_report = run_declarative_suite(args.suite_root)
            suite_payload = suite_report.to_dict()
            if args.report is not None:
                args.report.write_text(
                    json.dumps(suite_payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            _write_json(suite_payload)
            return 0
        if args.command == "claim-suite-validate":
            _write_json(validate_local_claim_suite(args.suite_root).to_dict())
            return 0
        if args.command == "memory":
            memory_store = LongTermMemory(state_path=args.state)
            if args.memory_action == "propose":
                record = memory_store.propose(
                    sys.stdin.read(),
                    kind=args.kind,
                    scope=args.scope,
                    origin=args.origin,
                    source_message_ids=tuple(args.source_message_id),
                    expected_revision=args.expected_revision,
                )
                _write_json(_memory_mutation_result(memory_store, record))
                return 0
            if args.memory_action == "confirm":
                record = memory_store.confirm(
                    args.record_id,
                    scope=args.scope,
                    expected_revision=args.expected_revision,
                )
                _write_json(_memory_mutation_result(memory_store, record))
                return 0
            if args.memory_action == "deactivate":
                record = memory_store.deactivate(
                    args.record_id,
                    scope=args.scope,
                    expected_revision=args.expected_revision,
                )
                _write_json(_memory_mutation_result(memory_store, record))
                return 0
            if args.memory_action == "list":
                snapshot = memory_store.snapshot()
                _write_json({
                    "schema_version": snapshot.schema_version,
                    "revision": snapshot.revision,
                    "legacy_read_only": snapshot.legacy_read_only,
                    "records": [
                        _memory_record_metadata(record)
                        for record in memory_store.list_records(scope=args.scope, status=args.status)
                    ],
                })
                return 0
            if args.memory_action == "import-legacy":
                _write_json(memory_store.import_legacy(scope=args.scope, apply=args.apply).to_dict())
                return 0
        if args.command == "research-worker":
            corpus = Corpus.load(args.corpus)
            worker = ResearchWorker(
                ResearchTaskStore(args.task_db), corpus,
                worker_id=args.worker_id or f"worker-{uuid.uuid4().hex}",
                lease_seconds=args.lease_seconds,
            )
            previous_handler = signal.signal(signal.SIGTERM, _interrupt_worker)
            try:
                while True:
                    outcome = worker.run_once()
                    if args.once or outcome.status != "idle":
                        _write_json(outcome.to_dict())
                        sys.stdout.flush()
                    if args.once:
                        return 0 if outcome.status in {"succeeded", "idle"} else 1
                    if outcome.status == "idle":
                        time.sleep(0.25)
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
        if args.command == "serve":
            if args.port < 1 or args.port > 65535:
                raise ValueError("port 必须在 1..65535")
            try:
                import uvicorn
            except ImportError as exc:
                raise RuntimeError("serve extra 未安装") from exc
            from .api import create_app

            token = secrets.token_urlsafe(32)
            print(f"session token: {token}", file=sys.stderr)
            host = "0.0.0.0" if args.bind == "container" else "127.0.0.1"
            uvicorn.run(
                create_app(args.corpus, session_token=token, task_db=args.task_db),
                host=host, port=args.port, access_log=False,
            )
            return 0
    except KeyboardInterrupt:
        return 130
    except (
        CorpusError,
        EvaluationError,
        LocalClaimEvaluationError,
        LongTermMemoryError,
        RagEvaluationError,
        RagIndexError,
        ValueError,
        RuntimeError,
    ):
        print("error: agentlab_command_failed", file=sys.stderr)
        return 2
    return 2


def _write_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _interrupt_worker(_signum, _frame) -> None:
    raise KeyboardInterrupt


def _add_memory_state_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(".agentlab") / "longterm.json",
        help="local memory state path",
    )


def _memory_record_metadata(record: MemoryRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "kind": record.kind,
        "scope": record.scope,
        "status": record.status,
        "origin": record.origin,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "content_hash": record.content_hash,
        "source_message_count": len(record.source_message_ids),
    }


def _memory_mutation_result(memory: LongTermMemory, record: MemoryRecord) -> dict[str, object]:
    return {
        "revision": memory.snapshot().revision,
        "record": _memory_record_metadata(record),
    }


if __name__ == "__main__":
    raise SystemExit(main())
