"""Run or refresh the frozen controlled-memory canonical report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentlab.controlled_memory_evaluation import run_controlled_memory_evaluation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite-root",
        type=Path,
        default=Path("evals") / "controlled-memory-v1",
    )
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = run_controlled_memory_evaluation(args.suite_root)
    if args.write:
        (args.suite_root / "baseline-report.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.suite_root / "baseline-report.md").write_text(report.to_markdown(), encoding="utf-8")
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
