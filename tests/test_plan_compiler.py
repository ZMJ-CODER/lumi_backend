import asyncio
from types import SimpleNamespace

from app.agents.orchestration import plan_compiler
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.plan_compiler import (
    CapabilitySnapshot,
    CompileDecision,
    compile_plan,
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
