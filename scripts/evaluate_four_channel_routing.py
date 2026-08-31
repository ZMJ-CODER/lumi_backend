"""Evaluate the deterministic four-channel routing policy without executing work.

The fixture is a labelled, representative coverage set.  Its intentionally
balanced channel distribution validates routing correctness; it is *not* a
claim about real production traffic share.  Production share must be measured
separately by aggregating FOUR_CHANNEL_ROUTE_DECISION telemetry.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agents.orchestration.task_routing import RouteChannel, route_atomic_instruction


DEFAULT_FIXTURE = Path("tests/fixtures/four_channel_routing_eval.jsonl")
DEFAULT_OUTPUT = Path("artifacts/four-channel-routing-eval")


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load a small JSONL oracle without sending requests to an LLM or workers."""
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = str(row.get("id") or "").strip()
        instruction = str(row.get("instruction") or "").strip()
        expected_route = str(row.get("expected_route") or "").strip()
        if not case_id or not instruction or expected_route not in {channel.value for channel in RouteChannel}:
            raise ValueError(f"{path}:{line_number} 缺少 id/instruction 或 expected_route 非法")
        if case_id in seen:
            raise ValueError(f"{path}:{line_number} 存在重复 id: {case_id}")
        seen.add(case_id)
        cases.append({
            "id": case_id,
            "instruction": instruction,
            "expected_route": expected_route,
            "has_authorized_documents": bool(row.get("has_authorized_documents", False)),
            "office_document_count": max(0, int(row.get("office_document_count") or 0)),
        })
    if not cases:
        raise ValueError(f"评测集为空: {path}")
    return cases


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Return exact-route accuracy, confusion matrix, and route/token shares."""
    channels = [channel.value for channel in RouteChannel]
    predicted_counts: Counter[str] = Counter()
    expected_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    predicted_tokens: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    records: list[dict[str, Any]] = []
    for case in cases:
        decision = route_atomic_instruction(
            case["instruction"],
            has_authorized_documents=case["has_authorized_documents"],
            office_document_count=case["office_document_count"],
        )
        predicted = decision.channel.value
        expected = case["expected_route"]
        expected_counts[expected] += 1
        predicted_counts[predicted] += 1
        predicted_tokens[predicted] += decision.estimated_tokens
        reason_counts[decision.reason] += 1
        confusion[expected][predicted] += 1
        records.append({
            **case,
            "actual_route": predicted,
            "route_correct": predicted == expected,
            "reason": decision.reason,
            "estimated_tokens": decision.estimated_tokens,
        })
    total = len(records)
    total_tokens = sum(predicted_tokens.values())
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_kind": "labelled_representative_coverage_not_production_telemetry",
        "case_count": total,
        "route_accuracy": round(sum(row["route_correct"] for row in records) / total, 4),
        "expected_route_counts": {channel: expected_counts[channel] for channel in channels},
        "actual_route_counts": {channel: predicted_counts[channel] for channel in channels},
        "actual_route_shares": {channel: round(predicted_counts[channel] / total, 4) for channel in channels},
        "estimated_token_counts": {channel: predicted_tokens[channel] for channel in channels},
        "estimated_token_shares": {
            channel: round(predicted_tokens[channel] / total_tokens, 4) if total_tokens else 0.0
            for channel in channels
        },
        "reason_counts": dict(sorted(reason_counts.items())),
        "confusion_matrix": {
            expected: {actual: confusion[expected][actual] for actual in channels}
            for expected in channels
        },
        "records": records,
    }


def render_markdown(report: dict[str, Any]) -> str:
    channels = [channel.value for channel in RouteChannel]
    lines = [
        "# Four-channel routing evaluation",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- cases: {report['case_count']}",
        f"- exact route accuracy: {report['route_accuracy']:.2%}",
        "- scope: deterministic route classification only; no LLM, retrieval, script, agent, or external operation was executed.",
        "- caveat: the fixture balances channels for coverage. Its proportions are not production traffic proportions.",
        "",
        "| route | expected cases | actual cases | actual share | estimated token share |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for channel in channels:
        lines.append(
            f"| {channel} | {report['expected_route_counts'][channel]} | "
            f"{report['actual_route_counts'][channel]} | {report['actual_route_shares'][channel]:.2%} | "
            f"{report['estimated_token_shares'][channel]:.2%} |"
        )
    lines.extend(["", "## Confusion matrix", "", "| expected \\ actual | " + " | ".join(channels) + " |", "| --- | " + " | ".join(["---:"] * len(channels)) + " |"])
    for expected in channels:
        lines.append("| " + expected + " | " + " | ".join(str(report["confusion_matrix"][expected][actual]) for actual in channels) + " |")
    lines.extend(["", "## Reasons", ""])
    lines.extend(f"- {reason}: {count}" for reason, count in report["reason_counts"].items())
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline four-channel routing evaluation")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.fixture.is_file():
        parser.error(f"找不到评测集: {args.fixture}")
    report = evaluate_cases(load_cases(args.fixture))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "records"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
