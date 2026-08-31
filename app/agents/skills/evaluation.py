"""Deterministic scoring utilities for non-executing tool-routing evaluations.

The evaluator deliberately separates the two failure domains in a tool call:
the candidate router may fail to expose the expected tool, or the model may
select/parameterize the wrong tool after it was exposed.  It does not execute
tools and therefore is safe for fixtures containing write, desktop or network
requests.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.skills.base import SkillResult
from app.agents.skills.capability import ToolCapability


@dataclass(frozen=True)
class EvaluationTool:
    """A side-effect-free stand-in for one production tool contract.

    The model receives the production capability's name, description and JSON
    Schema unchanged.  If an evaluation needs to advance past a function call,
    this stand-in returns a fixed synthetic result instead of dispatching to
    the production Skill, MCP client, filesystem, desktop, database or network.
    """

    capability: ToolCapability

    @property
    def name(self) -> str:
        return self.capability.name

    def to_tool_definition(self) -> dict[str, Any]:
        """Return exactly the callable definition exposed by production routing."""
        return self.capability.to_tool_definition()

    async def execute(self, params: Mapping[str, Any] | None = None) -> SkillResult:
        """Return a constant test result and intentionally ignore all arguments."""
        return SkillResult(
            success=True,
            output=f"[evaluation] {self.name} executed with a synthetic result",
            metadata={"evaluation": True, "tool": self.name, "side_effects": "disabled"},
        )


def build_evaluation_tools(capabilities: Iterable[ToolCapability]) -> list[EvaluationTool]:
    """Clone callable contracts into safe test doubles without changing names."""
    return [EvaluationTool(capability) for capability in capabilities]


def load_eval_cases(path: Path) -> list[dict[str, Any]]:
    """Load a JSON array or JSONL fixture and perform the minimal contract checks."""
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
    if not isinstance(rows, list):
        raise ValueError("评测集根节点必须是 JSON 数组或 JSONL 记录")

    seen: set[str] = set()
    cases: list[dict[str, Any]] = []
    for index, value in enumerate(rows, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"第 {index} 条不是对象")
        case = dict(value)
        case_id = str(case.get("id") or "").strip()
        query = str(case.get("query") or "").strip()
        expected_tool = case.get("expected_tool")
        if not case_id or not query:
            raise ValueError(f"第 {index} 条缺少 id 或 query")
        if case_id in seen:
            raise ValueError(f"评测集存在重复 id: {case_id}")
        if expected_tool is not None and not isinstance(expected_tool, str):
            raise ValueError(f"{case_id}: expected_tool 必须是字符串或 null")
        expected_params = case.get("expected_params", {})
        if expected_tool is None and expected_params:
            raise ValueError(f"{case_id}: expected_tool 为 null 时 expected_params 必须为空")
        if not isinstance(expected_params, dict):
            raise ValueError(f"{case_id}: expected_params 必须是对象")
        case["expected_tool"] = expected_tool or None
        case["expected_params"] = expected_params
        case["scene"] = str(case.get("scene") or "chat")
        case["must_not_call"] = bool(case.get("must_not_call", expected_tool is None))
        seen.add(case_id)
        cases.append(case)
    return cases


def canonicalize_json(value: Any) -> Any:
    """Canonical JSON tree for BFCL-style structural equality.

    Dict key order is ignored, list order remains meaningful, and integer / float
    values compare numerically where safe.  No semantic or fuzzy matching is
    performed: a changed parameter name or value is an evaluation failure.
    """
    if isinstance(value, Mapping):
        return {str(key): canonicalize_json(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [canonicalize_json(item) for item in value]
    if isinstance(value, tuple):
        return [canonicalize_json(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return int(value) if value.is_integer() else value
    return value


def parse_tool_arguments(value: Any) -> dict[str, Any]:
    """Normalize OpenAI-compatible tool arguments without accepting malformed JSON."""
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("工具 arguments 必须是 JSON 对象")
    return value


def match_params(
    actual: Any,
    expected: Mapping[str, Any],
    *,
    allow_extra_params: bool = False,
) -> bool:
    """Compare parameter names and JSON values structurally.

    Default behavior is exact-object matching.  A fixture may opt in to
    ``allow_extra_params`` only for providers that inject schema defaults.
    """
    try:
        actual_params = parse_tool_arguments(actual)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    actual_tree = canonicalize_json(actual_params)
    expected_tree = canonicalize_json(dict(expected))
    if not allow_extra_params:
        return actual_tree == expected_tree
    return all(actual_tree.get(key, object()) == value for key, value in expected_tree.items())


def parameter_mismatch_reasons(actual: Any, expected: Mapping[str, Any]) -> list[str]:
    """Classify strict JSON mismatches without weakening the BFCL-style score."""
    try:
        actual_params = canonicalize_json(parse_tool_arguments(actual))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ["malformed_arguments"]
    expected_params = canonicalize_json(dict(expected))
    reasons: list[str] = []
    expected_keys, actual_keys = set(expected_params), set(actual_params)
    if expected_keys - actual_keys:
        reasons.append("missing_expected_field")
    if actual_keys - expected_keys:
        reasons.append("unexpected_field")
    for key in expected_keys & actual_keys:
        expected_value, actual_value = expected_params[key], actual_params[key]
        if expected_value == actual_value:
            continue
        if isinstance(expected_value, str) and isinstance(actual_value, str):
            if "".join(expected_value.split()) == "".join(actual_value.split()):
                reasons.append("string_whitespace_changed")
            else:
                reasons.append("string_value_changed")
        else:
            reasons.append("non_string_value_changed")
    return sorted(set(reasons)) or ["unknown_mismatch"]


def evaluate_tool_case(
    case: Mapping[str, Any],
    *,
    actual_tool: str | None,
    actual_params: Any = None,
    injected_candidates: Iterable[str] = (),
    usage: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Score one selection-only run.  No tool invocation occurs here."""
    expected_tool = case.get("expected_tool") or None
    candidate_names = [str(name) for name in injected_candidates if str(name)]
    actual_tool = str(actual_tool).strip() if actual_tool else None
    is_negative = bool(case.get("must_not_call", expected_tool is None))
    recall_hit = expected_tool in candidate_names if expected_tool else None
    tool_correct = actual_tool is None if expected_tool is None else actual_tool == expected_tool
    params_correct: bool | None
    if expected_tool is None:
        params_correct = None
    elif actual_tool != expected_tool:
        params_correct = False
    else:
        params_correct = match_params(
            actual_params,
            case.get("expected_params") or {},
            allow_extra_params=bool(case.get("allow_extra_params", False)),
        )
    fully_correct = bool(tool_correct and (params_correct is not False))
    token_usage = dict(usage or {})
    prompt_tokens = int(token_usage.get("prompt_tokens") or 0)
    completion_tokens = int(token_usage.get("completion_tokens") or 0)
    return {
        "case_id": str(case.get("id") or ""),
        "query": str(case.get("query") or ""),
        "scene": str(case.get("scene") or "chat"),
        "expected_tool": expected_tool,
        "expected_params": canonicalize_json(case.get("expected_params") or {}),
        "actual_tool": actual_tool,
        "actual_params": canonicalize_json(actual_params or {}),
        "injected_candidates": candidate_names,
        "candidate_recall_hit": recall_hit,
        "tool_correct": tool_correct,
        "params_correct": params_correct,
        "parameter_mismatch_reasons": (
            [] if params_correct is not False else parameter_mismatch_reasons(actual_params, case.get("expected_params") or {})
        ),
        "fully_correct": fully_correct,
        "must_not_call": is_negative,
        "false_call": bool(is_negative and actual_tool is not None),
        "llm_calls": int(token_usage.get("llm_calls") or 0),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(token_usage.get("total_tokens") or prompt_tokens + completion_tokens),
        "model": str(token_usage.get("model") or ""),
        "prompt_token_source": str(token_usage.get("prompt_token_source") or ""),
        "completion_token_source": str(token_usage.get("completion_token_source") or ""),
        "error": str(error or ""),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def compute_metrics(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute selection, candidate-recall and false-call metrics separately."""
    rows = list(records)
    successful_rows = [row for row in rows if not bool(row.get("error"))]
    positives = [row for row in rows if row.get("expected_tool")]
    negatives = [row for row in rows if bool(row.get("must_not_call"))]
    recalled = [row for row in positives if row.get("candidate_recall_hit") is True]
    successful_positives = [row for row in successful_rows if row.get("expected_tool")]
    successful_negatives = [row for row in successful_rows if bool(row.get("must_not_call"))]
    recalled_successful = [row for row in successful_positives if row.get("candidate_recall_hit") is True]
    correct_tool = [row for row in successful_rows if row.get("tool_correct") is True]
    correct_positive_tool = [row for row in successful_positives if row.get("tool_correct") is True]
    param_evaluable = [row for row in successful_positives if row.get("actual_tool") == row.get("expected_tool")]
    parameter_mismatch_reasons = Counter(
        reason
        for row in param_evaluable
        if row.get("params_correct") is False
        for reason in (row.get("parameter_mismatch_reasons") or [])
    )
    parameter_metrics_by_tool: dict[str, dict[str, Any]] = {}
    for tool_name in sorted({str(row["expected_tool"]) for row in param_evaluable}):
        tool_rows = [row for row in param_evaluable if str(row.get("expected_tool")) == tool_name]
        mismatches = Counter(
            reason
            for row in tool_rows
            if row.get("params_correct") is False
            for reason in (row.get("parameter_mismatch_reasons") or [])
        )
        parameter_metrics_by_tool[tool_name] = {
            "evaluated": len(tool_rows),
            "strictly_correct": sum(row.get("params_correct") is True for row in tool_rows),
            "accuracy": _rate(sum(row.get("params_correct") is True for row in tool_rows), len(tool_rows)),
            "mismatch_reasons": dict(sorted(mismatches.items())),
        }
    return {
        "case_count": len(rows),
        "valid_decision_count": len(successful_rows),
        "error_rate": _rate(len(rows) - len(successful_rows), len(rows)),
        "positive_case_count": len(positives),
        "negative_case_count": len(negatives),
        "candidate_recall_at_k": _rate(len(recalled), len(positives)),
        "candidate_recall_hits": len(recalled),
        "selection_accuracy_given_candidates": _rate(
            sum(row.get("tool_correct") is True for row in recalled_successful), len(recalled_successful)
        ),
        "tool_selection_accuracy": _rate(len(correct_tool), len(successful_rows)),
        "positive_tool_selection_accuracy": _rate(len(correct_positive_tool), len(successful_positives)),
        "parameter_accuracy_given_correct_tool": _rate(
            sum(row.get("params_correct") is True for row in param_evaluable), len(param_evaluable)
        ),
        "parameter_mismatch_reasons": dict(sorted(parameter_mismatch_reasons.items())),
        "parameter_metrics_by_tool": parameter_metrics_by_tool,
        "fully_correct_rate": _rate(sum(row.get("fully_correct") is True for row in successful_rows), len(successful_rows)),
        "false_call_rate": _rate(sum(row.get("false_call") is True for row in successful_negatives), len(successful_negatives)),
        "false_calls": sum(row.get("false_call") is True for row in successful_negatives),
        "llm_calls": sum(int(row.get("llm_calls") or 0) for row in rows),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in rows),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in rows),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in rows),
        "errors": sum(bool(row.get("error")) for row in rows),
        "expected_tool_coverage": dict(sorted(Counter(str(row["expected_tool"]) for row in positives).items())),
    }


def estimate_cost(
    metrics: Mapping[str, Any],
    pricing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a per-million-token pricing table; return ``None`` cost if absent."""
    pricing = dict(pricing or {})
    input_price = pricing.get("input_per_million")
    output_price = pricing.get("output_per_million")
    if not isinstance(input_price, (int, float)) or not isinstance(output_price, (int, float)):
        return {"currency": pricing.get("currency"), "estimated_cost": None, "pricing_available": False}
    cost = (
        float(metrics.get("prompt_tokens") or 0) / 1_000_000 * float(input_price)
        + float(metrics.get("completion_tokens") or 0) / 1_000_000 * float(output_price)
    )
    return {
        "currency": pricing.get("currency", "USD"),
        "estimated_cost": round(cost, 8),
        "pricing_available": True,
        "input_per_million": float(input_price),
        "output_per_million": float(output_price),
    }


def compare_costs(baseline: Mapping[str, Any], routed: Mapping[str, Any]) -> dict[str, Any]:
    """Compare token and monetary cost, preserving absent pricing as unknown."""
    base_tokens = int(baseline.get("total_tokens") or 0)
    routed_tokens = int(routed.get("total_tokens") or 0)
    base_cost = baseline.get("estimated_cost")
    routed_cost = routed.get("estimated_cost")
    token_reduction = 1 - routed_tokens / base_tokens if base_tokens else None
    cost_reduction = (
        1 - float(routed_cost) / float(base_cost)
        if isinstance(base_cost, (int, float)) and base_cost > 0 and isinstance(routed_cost, (int, float))
        else None
    )
    return {
        "token_reduction_ratio": round(token_reduction, 4) if token_reduction is not None else None,
        "cost_reduction_ratio": round(cost_reduction, 4) if cost_reduction is not None else None,
        "baseline_total_tokens": base_tokens,
        "routed_total_tokens": routed_tokens,
    }
