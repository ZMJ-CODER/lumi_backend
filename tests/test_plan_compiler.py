import asyncio
from types import SimpleNamespace

from app.agents.orchestration import plan_compiler
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.plan_compilation_service import PlanCompilationService
from app.agents.orchestration.plan_context import PlanRequestContext
from app.agents.orchestration.plan_compiler import (
    CapabilitySnapshot,
    CompileDecision,
    compile_plan,
    validate_request_coverage,
)


def _snapshot(*, tools=None):
    return CapabilitySnapshot(
        scene="office",
        user_role="user",
        workers=["atomic_step"],
        tools=tools or {
            "python_exec": {"parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
            "compliance_check": {"parameters": {"type": "object", "properties": {"text": {"type": "string"}}}},
            "open_app": {"parameters": {"type": "object", "properties": {"app": {"type": "string"}}}},
        },
        fingerprint="test-capabilities",
    )


def _node(node_id, tool, deps=(), inputs=None):
    return TaskNode(
        id=node_id,
        agent="atomic_step",
        name=node_id,
        params={
            "instruction": node_id,
            "preferred_tool": tool,
            "fallback_tools": [],
            "inputs": inputs or {},
        },
        depends_on=list(deps),
    )


def _compile(monkeypatch, nodes, snapshot=None):
    async def fake_snapshot(**_kwargs):
        return snapshot or _snapshot()

    monkeypatch.setattr(plan_compiler, "build_capability_snapshot", fake_snapshot)
    return asyncio.run(compile_plan(
        nodes,
        scene="office",
        user_role="user",
        user_id="u1",
        workers={"atomic_step": SimpleNamespace()},
    ))


def test_compiler_accepts_explicit_three_step_plan(monkeypatch):
    result = _compile(monkeypatch, [
        _node("convert", "python_exec"),
        _node("judge", "compliance_check", deps=["convert"]),
        _node("open", "open_app", deps=["judge"]),
    ])

    assert result.decision in {CompileDecision.ACCEPTED, CompileDecision.NORMALIZED}
    assert [node.id for node in result.nodes] == ["convert", "judge", "open"]
    assert result.cost.critical_path_ms > 0


def test_compiler_keeps_oversized_plan_for_logical_window(monkeypatch):
    """A valid long plan is rolled, not rejected or silently truncated."""
    result = _compile(monkeypatch, [
        _node(f"step-{index}", ["python_exec", "compliance_check", "open_app"][index % 3])
        for index in range(9)
    ])

    assert result.decision == CompileDecision.NORMALIZED
    assert len(result.nodes) == 9
    assert any(item.code == "NODE_LIMIT" for item in result.warnings)


def test_compiler_rejects_cycle_and_does_not_truncate(monkeypatch):
    result = _compile(monkeypatch, [
        _node("a", "python_exec", deps=["b"]),
        _node("b", "compliance_check", deps=["a"]),
    ])

    assert result.decision == CompileDecision.REPLAN_REQUIRED
    assert any(item.code == "DAG_INVALID" for item in result.violations)
    assert len(result.nodes) == 2


def test_compiler_rejects_unavailable_preferred_tool(monkeypatch):
    result = _compile(monkeypatch, [_node("convert", "missing_tool")])

    assert result.decision == CompileDecision.REPLAN_REQUIRED
    assert any(item.code == "TOOL_UNAVAILABLE" for item in result.violations)


def test_compiler_rejects_malformed_explicit_tool_input(monkeypatch):
    result = _compile(
        monkeypatch,
        [_node("convert", "python_exec", inputs={"path": 123})],
    )

    assert result.decision == CompileDecision.REPLAN_REQUIRED
    assert any(item.code == "PARAM_TYPE" for item in result.violations)


def test_request_coverage_catches_dropped_multilingual_deliverables():
    partial = TaskNode(
        id="t1",
        name="提炼重点",
        agent="direct_llm",
        params={"instruction": "提炼 release note 的三个重点"},
    )

    violations = validate_request_coverage(
        "先提炼重点，然后 translate 成英文，最后做一个 Markdown table",
        [partial],
    )

    assert [item.code for item in violations] == ["PLAN_COVERAGE"]
    assert "translate" in violations[0].message
    assert "table" in violations[0].message


def test_request_coverage_accepts_one_node_when_it_contains_all_deliverables():
    complete = TaskNode(
        id="t1",
        name="完成请求",
        agent="direct_llm",
        params={"instruction": "提炼重点、translate 成英文并生成 Markdown table"},
    )

    assert validate_request_coverage(
        "先提炼重点，然后 translate 成英文，最后做一个 Markdown table",
        [complete],
    ) == []


def test_compilation_feedback_replans_when_request_action_is_dropped(monkeypatch):
    async def fake_snapshot(**_kwargs):
        return CapabilitySnapshot(
            scene="office", user_role="user", workers=["direct_llm"], tools={}, fingerprint="test"
        )

    monkeypatch.setattr(plan_compiler, "build_capability_snapshot", fake_snapshot)
    calls = []
    partial = type("Tree", (), {
        "nodes": [TaskNode(id="t1", name="提炼", agent="direct_llm", params={"instruction": "提炼重点"})],
        "error": None,
        "error_code": None,
        "plan_text": None,
    })()
    complete = type("Tree", (), {
        "nodes": [TaskNode(
            id="t1",
            name="提炼并翻译",
            agent="direct_llm",
            params={"instruction": "提炼重点、translate 成英文并生成 Markdown table"},
        )],
        "error": None,
        "error_code": None,
        "plan_text": None,
    })()

    async def replan(context):
        calls.append(context.prior_summaries)
        return complete

    async def scenario():
        service = PlanCompilationService(
            workers={"direct_llm": object()},
            plan_with_context=replan,
        )
        return await service.compile_with_feedback(
            partial,
            routing={},
            user_role="user",
            context=PlanRequestContext(
                user_id="u1",
                request="先提炼重点，然后 translate 成英文，最后做一个 Markdown table",
            ),
        )

    result = asyncio.run(scenario())
    assert result.error is None
    assert len(result.nodes) == 1
    assert calls and "PLAN_COVERAGE" not in calls[0]
