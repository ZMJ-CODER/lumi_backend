import asyncio

from app.agents.skills.evaluation import (
    EvaluationTool,
    compare_costs,
    compute_metrics,
    evaluate_tool_case,
    match_params,
    parameter_mismatch_reasons,
)
from app.agents.skills.capability import ToolCapability
from scripts.evaluate_tool_routing import _mock_decision


def test_match_params_is_structural_and_ignores_object_key_order():
    expected = {"query": "AI policy", "filters": {"year": 2026, "regions": ["CN", "EU"]}}

    assert match_params(
        {"filters": {"regions": ["CN", "EU"], "year": 2026.0}, "query": "AI policy"}, expected
    )
    assert not match_params({"query": "AI policy", "unexpected": True, "filters": expected["filters"]}, expected)
    assert match_params(
        {"query": "AI policy", "unexpected": True, "filters": expected["filters"]}, expected, allow_extra_params=True
    )
    assert not match_params({"query": "AI policy", "filters": {"year": 2026, "regions": ["EU", "CN"]}}, expected)


def test_parameter_mismatch_reasons_keep_strict_score_and_explain_default_omission():
    expected = {"query": "AI policy", "top_k": 5}

    assert parameter_mismatch_reasons({"query": "AI policy"}, expected) == ["missing_expected_field"]
    assert parameter_mismatch_reasons({"query": "AI  policy", "top_k": 5}, expected) == ["string_whitespace_changed"]
    assert parameter_mismatch_reasons({"query": "AI regulation", "top_k": 3}, expected) == [
        "non_string_value_changed",
        "string_value_changed",
    ]


def test_evaluation_tool_preserves_production_contract_but_never_dispatches():
    capability = ToolCapability(
        name="delete_file",
        description="删除用户电脑上的文件",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    tool = EvaluationTool(capability)

    assert tool.to_tool_definition() == capability.to_tool_definition()
    result = asyncio.run(tool.execute({"path": "C:/important.txt"}))

    assert result.success is True
    assert result.metadata["side_effects"] == "disabled"
    assert "delete_file" in result.output


def test_fixture_oracle_cannot_select_a_tool_that_was_not_injected():
    tool, params, _ = _mock_decision(
        {"expected_tool": "calculator", "expected_params": {"expression": "1+1"}}, []
    )

    assert tool is None
    assert params == {}


def test_metrics_separate_candidate_recall_model_selection_and_false_calls():
    positive_recalled = evaluate_tool_case(
        {"id": "positive-recalled", "query": "calculate", "expected_tool": "calculator", "expected_params": {"expression": "1+1"}},
        actual_tool="calculator",
        actual_params={"expression": "1+1"},
        injected_candidates=["calculator", "web_search"],
        usage={"llm_calls": 1, "prompt_tokens": 10, "completion_tokens": 2},
    )
    positive_missed = evaluate_tool_case(
        {"id": "positive-missed", "query": "now", "expected_tool": "get_datetime", "expected_params": {"format": "time"}},
        actual_tool="web_search",
        actual_params={"query": "current time"},
        injected_candidates=["web_search"],
        usage={"llm_calls": 1, "prompt_tokens": 10, "completion_tokens": 3},
    )
    negative = evaluate_tool_case(
        {"id": "negative", "query": "hello", "expected_tool": None, "expected_params": {}, "must_not_call": True},
        actual_tool="calculator",
        actual_params={"expression": "1+1"},
        injected_candidates=["calculator"],
        usage={"llm_calls": 1, "prompt_tokens": 10, "completion_tokens": 1},
    )

    metrics = compute_metrics([positive_recalled, positive_missed, negative])

    assert metrics["candidate_recall_at_k"] == 0.5
    assert metrics["selection_accuracy_given_candidates"] == 1.0
    assert metrics["positive_tool_selection_accuracy"] == 0.5
    assert metrics["tool_selection_accuracy"] == 0.3333
    assert metrics["parameter_accuracy_given_correct_tool"] == 1.0
    assert metrics["false_call_rate"] == 1.0
    assert metrics["total_tokens"] == 36
    assert metrics["parameter_metrics_by_tool"] == {
        "calculator": {
            "evaluated": 1,
            "strictly_correct": 1,
            "accuracy": 1.0,
            "mismatch_reasons": {},
        }
    }


def test_model_transport_errors_are_not_scored_as_model_mistakes():
    record = evaluate_tool_case(
        {"id": "offline", "query": "calculate", "expected_tool": "calculator", "expected_params": {"expression": "1+1"}},
        actual_tool=None,
        injected_candidates=["calculator"],
        error="APIConnectionError: Connection error.",
    )

    metrics = compute_metrics([record])

    assert metrics["candidate_recall_at_k"] == 1.0
    assert metrics["valid_decision_count"] == 0
    assert metrics["tool_selection_accuracy"] is None
    assert metrics["selection_accuracy_given_candidates"] is None
    assert metrics["error_rate"] == 1.0


def test_cost_comparison_uses_input_and_output_price_separately():
    baseline = {"total_tokens": 300, "estimated_cost": 0.0003}
    routed = {"total_tokens": 120, "estimated_cost": 0.00006}

    comparison = compare_costs(baseline, routed)

    assert comparison["token_reduction_ratio"] == 0.6
    assert comparison["cost_reduction_ratio"] == 0.8
