"""A 项回归：``SkillResult[T]`` 契约与 Skill 步骤账。

验证：
1. 契约四类对象齐备（ToolRequest / ExecutionResult / SkillResult / StreamEvent）；
2. ``SkillResult`` ↔ ``ExecutionResult`` 双向转换不丢控制信息；
3. 组合 Skill 执行后，每个内部工具调用都在 ``quality_hints["skill_steps"]`` 里有账；
4. 回程 ``ToolOutput`` 以原信封为底，不重建字段。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from lumi_contracts import (
    SKILL_RESULT,
    ExecutionStatus,
    SkillResult,
    SkillStep,
    ToolRequest,
    ExecutionResult,
    StreamEvent,
)

from app.agents.skills.base import SkillContext, ToolOutput
from app.contracts.skill_result import skill_result_to_tool_output, to_skill_result


def test_four_core_objects_exist_and_are_distinct():
    assert ToolRequest(tool_name="workspace_read").tool_name == "workspace_read"
    assert ExecutionResult[dict](payload={"a": 1}).payload == {"a": 1}
    assert SkillResult[dict](skill_name="s", payload={"a": 1}).payload == {"a": 1}
    assert StreamEvent(type="delta").type == "delta"
    assert str(SKILL_RESULT) == "lumi.skill_result@1"


def test_skill_result_roundtrip_through_execution_result():
    original = SkillResult[dict](
        skill_name="workspace_operation",
        status=ExecutionStatus.PARTIAL,
        payload={"files": ["a.txt"]},
        steps=[SkillStep(name="workspace_read", index=0, tool="workspace_read"), SkillStep(name="workspace_commit", index=1, status=ExecutionStatus.FAILED, error_code="CLIENT_REJECTED")],
        call_id="call-1",
        job_id="job-1",
    )
    execution = original.to_execution_result()
    assert execution.status is ExecutionStatus.PARTIAL
    assert execution.payload == {"files": ["a.txt"]}
    assert execution.tool_name == "workspace_operation"
    assert execution.call_id == "call-1"
    # 步骤账作为审计元数据随结果走，不进业务 payload
    assert len(execution.metadata["skill_steps"]) == 2

    back = SkillResult.from_execution_result(execution, skill_name="workspace_operation")
    assert back.status is ExecutionStatus.PARTIAL
    assert back.payload == {"files": ["a.txt"]}
    assert back.call_id == "call-1"


def test_step_counts_are_derived():
    result = SkillResult[Any](
        steps=[
            SkillStep(name="a", status=ExecutionStatus.SUCCESS),
            SkillStep(name="b", status=ExecutionStatus.FAILED),
            SkillStep(name="c", status=ExecutionStatus.SUCCESS),
        ]
    )
    assert result.step_counts() == {"success": 2, "failed": 1}


def test_to_skill_result_from_legacy_tool_output_keeps_control_info():
    legacy = ToolOutput(
        status="failed",
        call_id="call-9",
        data="boom",
        error="失败",
        error_code="EXEC_ERROR",
        retryable=True,
    )
    contract = to_skill_result(legacy, skill_name="demo_skill", steps=[SkillStep(name="t1", index=0)])
    assert contract.skill_name == "demo_skill"
    assert contract.status is ExecutionStatus.FAILED
    assert contract.retryable is True
    assert contract.error is not None and contract.error.code == "EXEC_ERROR"
    assert [step.name for step in contract.steps] == ["t1"]


def test_back_conversion_uses_base_envelope_and_only_adds_step_account():
    base = ToolOutput(
        status="success",
        call_id="call-3",
        data={"ok": True},
        content_type="structured",
        meta={"summary": "摘要", "workspace_id": "ws-9", "quality_hints": {"keep": 1}},
    )
    contract = to_skill_result(
        base,
        skill_name="demo_skill",
        steps=[SkillStep(name="workspace_read", index=0, tool="workspace_read", call_id="call-3")],
    )
    output = skill_result_to_tool_output(contract, base=base)
    assert output.status == "success"
    assert output.data == {"ok": True}
    assert output.call_id == "call-3"
    # meta 扩展字段不被重建掉
    assert output.meta.workspace_id == "ws-9"
    assert output.meta.summary == "摘要"
    hints = output.meta.quality_hints
    assert hints["keep"] == 1
    assert hints["skill"] == "demo_skill"
    assert hints["skill_steps"][0]["tool"] == "workspace_read"


def test_back_conversion_without_base_builds_minimal_envelope():
    contract = SkillResult[dict](
        skill_name="demo_skill",
        status=ExecutionStatus.FAILED,
        error=None,
        payload={"x": 1},
        steps=[],
    )
    output = skill_result_to_tool_output(contract)
    assert output.status == "failed"
    assert output.content_type == "structured"
    assert output.data == {"x": 1}
    assert output.meta.quality_hints["skill"] == "demo_skill"


# ── 真实组合 Skill 的步骤账 ─────────────────────────────────────


class _FakeWorkflow:
    """最小可用 WorkflowSkill：调用两次内部工具。"""

    name = "demo_flow"
    allowed_tools = ()

    def effective_dependencies(self):
        return {}

    async def run(self, params, context, invoke_tool):
        first = await invoke_tool("workspace_read", {"path": "a.txt"})
        second = await invoke_tool("workspace_search", {"query": "x"})
        return ToolOutput(success=True, output=f"{first.status}/{second.status}")


@pytest.fixture()
def _patched_runner(monkeypatch):
    """替换能力表与依赖解析（真实 Electron/注册表在单测里不可用）。"""
    import app.agents.skills.executor as executor

    async def fake_capabilities(*_args, **_kwargs):
        return []

    async def fake_desktop(*_args, **_kwargs):
        return []

    monkeypatch.setattr(executor, "get_capabilities_for_scene", fake_capabilities)
    monkeypatch.setattr(executor, "get_desktop_mcp_capabilities", fake_desktop)

    class _Report:
        required_issues: list = []

        def as_dict(self):
            return {}

    monkeypatch.setattr(
        "app.agents.skills.dependencies.resolve_dependencies", lambda *a, **k: _Report()
    )
    monkeypatch.setattr(
        "app.agents.skills.dependencies.synthesize_aggregated_capabilities", lambda m: {}
    )


def test_workflow_skill_records_one_step_per_tool_call(_patched_runner, monkeypatch):
    import app.agents.skills.workflow_runner as runner

    calls = []

    async def fake_execute(tool_call, *args, **kwargs):
        name = tool_call["function"]["name"]
        calls.append(name)
        return ToolOutput(status="success", output=f"ok:{name}", call_id=tool_call["id"])

    monkeypatch.setattr(runner, "execute_tool_call", fake_execute)
    context = SkillContext(user_id="u1", scene="office", conversation_id="c1")
    # workspace_* 工具要求服务端已选工作区（工作区 ID 是不可由模型切换的权威值）。
    result = asyncio.run(
        runner.run_workflow_skill(_FakeWorkflow(), {}, context, workspace_id="ws-1")
    )

    assert result.status == "success"
    assert calls == ["workspace_read", "workspace_search"]
    steps = result.meta.quality_hints["skill_steps"]
    assert [row["tool"] for row in steps] == ["workspace_read", "workspace_search"]
    assert all(row["status"] == "success" for row in steps)
    assert [row["index"] for row in steps] == [0, 1]
    assert result.meta.quality_hints["skill"] == "demo_flow"
