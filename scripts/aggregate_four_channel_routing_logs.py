"""Aggregate content-free FOUR_CHANNEL_ROUTE_DECISION log records.

    The parser accepts one or more Loguru log files and only reads the route,
    reason and estimated token fields emitted by task_manifest. It deliberately
    does not reconstruct user instructions from logs.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


EVENT_PREFIX = "FOUR_CHANNEL_ROUTE_DECISION "
EVENT_PATTERN = re.compile(re.escape(EVENT_PREFIX) + r"(\{.*\})")
VALID_ROUTES = ("direct_llm", "deterministic_script", "rag", "agent")


def aggregate(
    paths: list[Path], *, minimum_events: int = 100, only_evaluation_dry_run: bool = False
) -> dict:
    counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    malformed = 0
    events = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = EVENT_PATTERN.search(line)
            if not match:
                continue
            try:
                event = json.loads(match.group(1))
                route = str(event["route"])
                if route not in VALID_ROUTES:
                    raise ValueError("unknown route")
                estimated_tokens = max(0, int(event.get("estimated_tokens") or 0))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                malformed += 1
                continue
            if only_evaluation_dry_run and event.get("evaluation_dry_run") is not True:
                continue
            events += 1
            counts[route] += 1
            token_counts[route] += estimated_tokens
            reasons[str(event.get("reason") or "unknown")] += 1
    total_tokens = sum(token_counts.values())
    return {
        "source_files": [str(path) for path in paths],
        "event_count": events,
        "minimum_events": minimum_events,
        "only_evaluation_dry_run": only_evaluation_dry_run,
        "sample_sufficient": events >= minimum_events,
        "malformed_event_count": malformed,
        "route_counts": {route: counts[route] for route in VALID_ROUTES},
        "route_shares": {route: round(counts[route] / events, 4) if events else 0.0 for route in VALID_ROUTES},
        "estimated_token_counts": {route: token_counts[route] for route in VALID_ROUTES},
        "estimated_token_shares": {
            route: round(token_counts[route] / total_tokens, 4) if total_tokens else 0.0 for route in VALID_ROUTES
        },
        "reason_counts": dict(sorted(reasons.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate four-channel routing telemetry")
    parser.add_argument("logs", nargs="+", type=Path, help="one or more application log files")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--minimum-events",
        type=int,
        default=100,
        help="最低解释样本量；仅在事件数达到该值时 sample_sufficient=true",
    )
    parser.add_argument(
        "--only-evaluation-dry-run",
        action="store_true",
        help="仅汇总历史真实 LLM 路由评测 dry-run 事件（已归档）",
    )
    args = parser.parse_args()
    missing = [path for path in args.logs if not path.is_file()]
    if missing:
        parser.error("找不到日志文件: " + ", ".join(str(path) for path in missing))
    if args.minimum_events < 1:
        parser.error("--minimum-events 必须大于等于 1")
    report = aggregate(
        args.logs,
        minimum_events=args.minimum_events,
        only_evaluation_dry_run=args.only_evaluation_dry_run,
    )
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
