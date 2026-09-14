"""汇总路由标注样本中的 L2 margin 与 L3 confidence_hint。"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.agents.orchestration.planning.confidence_calibration import expected_calibration_error, fit_temperature


def _first(record: dict[str, Any], name: str) -> Any:
    if name in record:
        return record[name]
    for parent in (record.get("selection"), record.get("metadata"), record.get("routing")):
        if isinstance(parent, dict) and name in parent:
            return parent[name]
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("routing"), dict):
        return metadata["routing"].get(name)
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _margin(record: dict[str, Any]) -> float | None:
    value = _number(_first(record, "score_margin"))
    if value is not None:
        return value
    top = _number(_first(record, "top_score"))
    second = _number(_first(record, "second_score"))
    return top - second if top is not None and second is not None else None


def _bin_margin(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 1:
        return "[0,1)"
    if value < 3:
        return "[1,3)"
    if value < 5:
        return "[3,5)"
    if value < 10:
        return "[5,10)"
    return "[10,+inf)"


def evaluate(path: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(value, dict) or not isinstance(_first(value, "correct"), bool):
            malformed += 1
            continue
        records.append(value)
    labels = [bool(_first(item, "correct")) for item in records]
    hints = [_number(_first(item, "confidence_hint")) for item in records]
    hint_pairs = [(hint, label) for hint, label in zip(hints, labels, strict=False) if hint is not None]
    margin_groups: dict[str, list[bool]] = defaultdict(list)
    for item, label in zip(records, labels, strict=False):
        margin_groups[_bin_margin(_margin(item))].append(label)
    result: dict[str, Any] = {
        "source": str(path),
        "sample_count": len(records),
        "malformed_count": malformed,
        "correct_count": sum(labels),
        "accuracy": sum(labels) / len(labels) if labels else 0.0,
        "margin_bins": {
            key: {"count": len(values), "accuracy": sum(values) / len(values)}
            for key, values in sorted(margin_groups.items())
        },
        "confidence_hint": {"sample_count": len(hint_pairs)},
    }
    if hint_pairs:
        probabilities = [pair[0] for pair in hint_pairs]
        targets = [pair[1] for pair in hint_pairs]
        calibration = fit_temperature(probabilities, targets)
        result["confidence_hint"].update({
            "ece": expected_calibration_error(probabilities, targets),
            "brier": calibration.before_brier,
            "temperature": calibration.temperature,
            "calibrated_ece": calibration.after_ece,
            "calibrated_brier": calibration.after_brier,
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="评估 L2 margin 与 L3 confidence_hint")
    parser.add_argument("input", type=Path, help="标注 JSONL")
    parser.add_argument("--output", type=Path, required=True, help="报告 JSON")
    args = parser.parse_args()
    report = evaluate(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
