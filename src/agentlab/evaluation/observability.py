"""Multi-run aggregation with an explicit conversion-proxy boundary."""

from __future__ import annotations

from typing import Sequence

from .metrics import CaseResult


def aggregate_runs(runs: Sequence[Sequence[CaseResult]]) -> dict[str, object]:
    total = 0
    ok = 0
    per_tag: dict[str, tuple[int, int]] = {}
    for run in runs:
        for result in run:
            total += 1
            ok += 1 if result.ok else 0
            for tag in result.case.tags:
                seen, passed = per_tag.get(tag, (0, 0))
                per_tag[tag] = (seen + 1, passed + (1 if result.ok else 0))
    conversion_proxy = ok / total if total else 0.0
    return {
        "runs": len(runs),
        "total_cases": total,
        "ok": ok,
        "success_rate": ok / total if total else 0.0,
        "conversion_proxy": conversion_proxy,
        "conversion_proxy_boundary": "任务完成率代理业务转化；不表述真实业务转化或营收",
        "per_tag_success_rate": {
            tag: (passed / seen if seen else 0.0) for tag, (seen, passed) in sorted(per_tag.items())
        },
    }


def render_observability(runs: Sequence[Sequence[CaseResult]]) -> str:
    return str(aggregate_runs(runs))
