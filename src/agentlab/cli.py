"""Command-line interface with JSON stdout and diagnostics on stderr."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Sequence

from .llm import LLMBackend
from .research.agent import ResearchAgent
from .research.corpus import Corpus, CorpusError
from .research.evaluation import EvaluationError, run_frozen_evaluation
from .research.live import LiveResearchReasoner
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

    serve = subparsers.add_parser("serve", help="serve a fixed corpus over loopback")
    serve.add_argument("--corpus", required=True, type=Path)
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--bind", choices=("loopback", "container"), default="loopback")
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
            uvicorn.run(create_app(args.corpus, session_token=token), host=host, port=args.port)
            return 0
    except (
        CorpusError,
        EvaluationError,
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


if __name__ == "__main__":
    raise SystemExit(main())
