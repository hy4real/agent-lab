"""Run or verify the frozen RAGChecker metric-layer replication."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentlab.research.ragchecker_evaluation import (
    RagCheckerEvaluationError,
    load_ragchecker_fixture,
    render_ragchecker_report,
    run_ragchecker_evaluation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-root", required=True, type=Path)
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path)
    destination.add_argument("--check", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        fixture, corpus = load_ragchecker_fixture(args.suite_root)
        report = run_ragchecker_evaluation(fixture, corpus)
        rendered = render_ragchecker_report(report)
        if args.check is not None:
            if args.check.read_text(encoding="utf-8") != rendered:
                print("error: ragchecker_report_mismatch", file=sys.stderr)
                return 1
            print("ragchecker canonical report verified", file=sys.stderr)
        elif args.output is not None:
            if args.output.exists():
                print("error: ragchecker_report_exists", file=sys.stderr)
                return 2
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0 if report.passed else 1
    except (OSError, RagCheckerEvaluationError):
        print("error: ragchecker_evaluation_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
