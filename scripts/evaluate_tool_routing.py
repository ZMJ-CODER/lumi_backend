"""Run a selection-only tool-routing benchmark without executing tools.

Examples:

    # Fixture and reporting smoke test. Does not contact an LLM.
    uv run python scripts/evaluate_tool_routing.py --mode both

    # Real A/B measurement. Provider configuration comes from the existing
    # application settings; this command neither prints nor stores API keys.
    uv run python scripts/evaluate_tool_routing.py --mode both --live \
      --pricing tests/fixtures/llm_pricing.example.json

The ``baseline`` injects every legal chat tool.  ``routed`` injects only the
existing candidate router's selection.  Both modes stop after the first model
tool decision, so no filesystem, desktop, email, network or other tool side
effect can occur.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agents.skills.evaluation import (
    build_evaluation_tools,
    compare_costs,
    compute_metrics,
    estimate_cost,
    evaluate_tool_case,
    load_eval_cases,
)
from app.agents.skills.executor import get_chat_capabilities_with_trace, get_capabilities_for_scene
from app.agents.skills.prompting import build_tool_selection_contract
from app.agents.skills.registry import init_skills
from app.core.llm import LLMClient


DEFAULT_FIXTURE = Path("tests/fixtures/tool_routing_eval.jsonl")
DEFAULT_OUTPUT = Path("artifacts/tool-routing-eval")


def _messages(query: str, selection_contract: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是离线工具选择评测器。仅根据用户请求和已注入的候选工具决定是否调用工具。"
                "只有候选工具确实必要时才调用；不需要工具时直接给出简短文本，不要虚构工具。"
                "最多调用一个工具。此为选择测试，禁止解释测试规则。\n\n"
                + selection_contract
            ),
        },
        {"role": "user", "content": query},
    ]


def _tool_definitions(capabilities: list[Any]) -> list[dict[str, Any]]:
    """Use production contracts with synthetic, non-dispatching executors."""
    return [tool.to_tool_definition() for tool in build_evaluation_tools(capabilities)]


async def _execute_stub_if_called(
    capabilities: list[Any], actual_tool: str | None, actual_params: Any
) -> dict[str, Any]:
    """Execute exactly one matching test double, never a production Skill."""
    if not actual_tool:
        return {"executed": False, "success": None, "output": ""}
    stubs = {tool.name: tool for tool in build_evaluation_tools(capabilities)}
    stub = stubs.get(actual_tool)
    if stub is None:
        return {"executed": False, "success": False, "output": "unknown_tool"}
    result = await stub.execute(actual_params if isinstance(actual_params, dict) else {})
    return {"executed": True, "success": result.success, "output": result.output}


def _mock_decision(
    case: dict[str, Any], candidate_names: list[str]
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """Fixture validator only; deliberately labelled so it cannot be mistaken for model data."""
    tool = case.get("expected_tool")
    # An oracle may not call a tool that was not injected.  This keeps the
    # smoke test honest about candidate recall rather than fabricating an
    # impossible successful model choice.
    tool = tool if tool in candidate_names else None
    params = dict(case.get("expected_params") or {}) if tool else {}
    return tool, params, {
        "model": "fixture-oracle",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_token_source": "not_called",
        "completion_token_source": "not_called",
        "llm_calls": 0,
    }


async def _run_case(
    case: dict[str, Any],
    *,
    mode: str,
    live: bool,
    model: str | None,
    llm: LLMClient | None,
) -> dict[str, Any]:
    scene = str(case["scene"])
    if scene != "chat":
        raise ValueError(f"{case['id']}: 当前评测仅支持 chat 场景，收到 {scene!r}")
    if mode == "baseline":
        capabilities = await get_capabilities_for_scene(scene)
        candidates = [item.name for item in capabilities]
        candidate_reason = "all_legal_tools"
    else:
        selection = await get_chat_capabilities_with_trace(str(case["query"]))
        capabilities = selection.capabilities
        # ``get_chat_capabilities_with_trace`` deliberately reports only
        # positively supported candidates in ``capabilities``.  The raw trace
        # can be empty for a zero-score candidate, so evaluate the same set
        # actually injected into the model, never the pre-filter trace rows.
        candidates = [item.name for item in capabilities]
        candidate_reason = selection.reason

    if not live:
        actual_tool, actual_params, usage = _mock_decision(case, candidates)
        error = ""
    else:
        assert llm is not None
        try:
            _, calls, usage = await llm.chat_with_tools_with_usage(
                _messages(str(case["query"]), build_tool_selection_contract(capabilities)),
                _tool_definitions(capabilities),
                scene=scene,
                model=model,
                usage_category="tool_decision_eval",
                # Benchmarks must not inflate a user's persisted usage stats.
                record_usage_event=False,
            )
            usage["llm_calls"] = 1
            if len(calls) > 1:
                error = f"模型返回 {len(calls)} 个工具调用，评测仅允许一个"
            else:
                error = ""
            call = calls[0] if calls else {}
            function = call.get("function") or {}
            actual_tool = str(function.get("name") or "").strip() or None
            actual_params = function.get("arguments") or {}
        except Exception as exc:  # noqa: BLE001
            actual_tool, actual_params = None, {}
            usage = {
                "model": model or "",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "prompt_token_source": "unavailable",
                "completion_token_source": "unavailable",
                "llm_calls": 1,
            }
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
    stub_result = await _execute_stub_if_called(capabilities, actual_tool, actual_params)
    record = evaluate_tool_case(
        case,
        actual_tool=actual_tool,
        actual_params=actual_params,
        injected_candidates=candidates,
        usage=usage,
        error=error,
    )
    record["mode"] = mode
    record["candidate_reason"] = candidate_reason
    record["execution"] = "selection_only"
    record["stub_execution"] = stub_result
    return record


def _pricing_for_model(pricing: dict[str, Any], model: str) -> dict[str, Any]:
    models = pricing.get("models") if isinstance(pricing.get("models"), dict) else pricing
    value = models.get(model) or models.get("default") or {}
    return value if isinstance(value, dict) else {}


def _write_markdown(path: Path, reports: dict[str, dict[str, Any]], comparison: dict[str, Any] | None, live: bool) -> None:
    lines = [
        "# Tool-routing evaluation report",
        "",
        f"- generated_at: {datetime.now(UTC).isoformat()}",
        f"- execution: {'live model selection; tools were not executed' if live else 'fixture oracle smoke test; not a model metric'}",
        "- parameter scoring: exact JSON structure (key and value), with fixture-only explicit extra-key opt-in",
        "",
        "| mode | valid / cases | tool selection | candidate recall@K | selection given candidates | params given correct tool | false-call rate | errors | tokens | cost |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    def format_rate(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2%}"

    for mode, report in reports.items():
        metrics = report["metrics"]
        cost = report["cost"]
        money = "n/a" if cost["estimated_cost"] is None else f"{cost['currency']} {cost['estimated_cost']:.8f}"
        lines.append(
            f"| {mode} | {metrics['valid_decision_count']} / {metrics['case_count']} | {format_rate(metrics['tool_selection_accuracy'])} | "
            f"{format_rate(metrics['candidate_recall_at_k'])} | {format_rate(metrics['selection_accuracy_given_candidates'])} | "
            f"{format_rate(metrics['parameter_accuracy_given_correct_tool'])} | {format_rate(metrics['false_call_rate'])} | "
            f"{metrics['errors']} | {metrics['total_tokens']} | {money} |"
        )
    if comparison:
        token_reduction = comparison["token_reduction_ratio"]
        cost_reduction = comparison["cost_reduction_ratio"]
        lines.extend([
            "",
            "## A/B cost comparison",
            "",
            f"- token reduction: {'n/a' if token_reduction is None else f'{token_reduction:.2%}'}",
            f"- monetary cost reduction: {'n/a' if cost_reduction is None else f'{cost_reduction:.2%}'}",
            "- Scope: this compares full legal chat-tool injection against the current candidate-router injection. It is not a substitute for an end-to-end four-channel workflow cost benchmark.",
        ])
    for mode, report in reports.items():
        metrics = report["metrics"]
        by_tool = metrics.get("parameter_metrics_by_tool") or {}
        if not by_tool:
            continue
        lines.extend([
            "",
            f"## Strict parameter diagnostics: {mode}",
            "",
            "| tool | evaluated after correct selection | strictly correct | strict accuracy | mismatch reasons |",
            "| --- | ---: | ---: | ---: | --- |",
        ])
        for tool_name, detail in by_tool.items():
            reasons = detail.get("mismatch_reasons") or {}
            reason_text = ", ".join(f"{name}:{count}" for name, count in reasons.items()) or "-"
            accuracy = detail.get("accuracy")
            lines.append(
                f"| {tool_name} | {detail.get('evaluated', 0)} | {detail.get('strictly_correct', 0)} | "
                f"{'n/a' if accuracy is None else f'{float(accuracy):.2%}'} | {reason_text} |"
            )
    lines.extend([
        "",
        "## Reading the result",
        "",
        "- Candidate recall@K isolates router misses; selection given candidates isolates model choice after the correct tool was exposed.",
        "- False-call rate is calculated only over `must_not_call` cases.",
        "- Live token values come from provider usage when present; fallback estimates are marked in each JSON record.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    cases = load_eval_cases(args.fixture)
    init_skills()
    modes = [args.mode] if args.mode != "both" else ["baseline", "routed"]
    pricing = json.loads(args.pricing.read_text(encoding="utf-8")) if args.pricing else {}
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    reports: dict[str, dict[str, Any]] = {}
    llm = LLMClient() if args.live else None
    try:
        for mode in modes:
            records: list[dict[str, Any]] = []
            for case in cases:
                for _ in range(args.repeat):
                    records.append(await _run_case(case, mode=mode, live=args.live, model=args.model, llm=llm))
            metrics = compute_metrics(records)
            model = next((str(row["model"]) for row in records if row.get("model")), args.model or "")
            cost = estimate_cost(metrics, _pricing_for_model(pricing, model))
            report = {
                "schema_version": 1,
                "mode": mode,
                "live": bool(args.live),
                "fixture": str(args.fixture),
                "repeat": args.repeat,
                "safety": "selection_only_no_tool_execution",
                "metrics": metrics,
                "cost": cost,
                "records": records,
            }
            reports[mode] = report
            (output_dir / f"{mode}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"mode": mode, **metrics, **cost}, ensure_ascii=False))
    finally:
        if llm:
            await llm.close()
    comparison = None
    if "baseline" in reports and "routed" in reports:
        comparison = compare_costs(
            {**reports["baseline"]["metrics"], **reports["baseline"]["cost"]},
            {**reports["routed"]["metrics"], **reports["routed"]["cost"]},
        )
        (output_dir / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(output_dir / "report.md", reports, comparison, args.live)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Selection-only benchmark for tool routing")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--mode", choices=["baseline", "routed", "both"], default="both")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", help="Optional existing application model override; API keys stay in normal config")
    parser.add_argument("--pricing", type=Path, help="Optional model pricing JSON; never put credentials here")
    parser.add_argument("--live", action="store_true", help="Call the configured LLM. Without this, validates fixture/reporting only.")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat 必须大于等于 1")
    if not args.fixture.is_file():
        parser.error(f"找不到评测集: {args.fixture}")
    if args.pricing and not args.pricing.is_file():
        parser.error(f"找不到价格文件: {args.pricing}")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
