"""Desktop workspace approval is owned by Electron, not duplicated by the DAG."""

from __future__ import annotations

import asyncio

from app.agents.skills.base import SkillContext, ToolOutput, WorkflowSkill
from app.agents.skills.capability import ToolCapability
from app.agents.skills import workflow_runner


class _OneCommitWorkflow(WorkflowSkill):
    name = "test_one_commit_workflow"
    scenes = ["office"]
    execution_scope = "backend_orchestrates_client"
    allowed_tools = ["mcp__lumi_client__workspace_commit"]
    dependencies = {}

    async def run(self, params, context, invoke_tool):
        return await invoke_tool(
            "mcp__lumi_client__workspace_commit",
            {"base_version": 2, "idempotency_key": "commit-1"},
        )


def _commit_capability() -> ToolCapability:
    return ToolCapability(
        name="mcp__lumi_client__workspace_commit",
        source="mcp",
        environment="client",
        server="lumi_client",
        raw_name="workspace_commit",
        write_op=True,
        requires_confirmation=True,
        confirmation_mode="client",
        parameters={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "base_version": {"type": "integer"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["workspace_id", "base_version", "idempotency_key"],
        },
    )


def test_workflow_reuses_call_id_until_electron_approval_completes(monkeypatch):
    calls: list[str] = []

    async def fake_execute(_tool_call, *_args, mcp_call_id=None, **_kwargs):
        calls.append(str(mcp_call_id or ""))
        if len(calls) == 1:
            return ToolOutput(
                status="pending_approval",
                call_id=mcp_call_id,
                data={"base_version": 2},
            )
        return ToolOutput(status="success", call_id=mcp_call_id, output="已提交")

    async def fake_scene(*_args, **_kwargs):
        return []

    async def fake_desktop(*_args, **_kwargs):
        return [_commit_capability()]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(workflow_runner, "execute_tool_call", fake_execute)
    monkeypatch.setattr(workflow_runner.asyncio, "sleep", no_sleep)
    monkeypatch.setattr("app.agents.skills.executor.get_capabilities_for_scene", fake_scene)
    monkeypatch.setattr("app.agents.skills.executor.get_desktop_mcp_capabilities", fake_desktop)
    monkeypatch.setattr(workflow_runner.settings, "MCP_CLIENT_APPROVAL_WAIT_S", 5.0)

    result = asyncio.run(workflow_runner.run_workflow_skill(
        _OneCommitWorkflow(),
        {},
        SkillContext(user_id="u1", scene="office", conversation_id="job-1", job_id="job-1", workspace_id="ws-1"),
        workspace_id="ws-1",
    ))

    assert result.status == "success"
    assert result.output == "已提交"
    assert len(calls) == 2
    assert calls[0] and calls[0] == calls[1]


def test_client_workspace_approval_timeout_does_not_become_server_approval(monkeypatch):
    async def fake_execute(_tool_call, *_args, mcp_call_id=None, **_kwargs):
        return ToolOutput(status="pending_approval", call_id=mcp_call_id, data={"base_version": 2})

    async def fake_scene(*_args, **_kwargs):
        return []

    async def fake_desktop(*_args, **_kwargs):
        return [_commit_capability()]

    async def no_sleep(_seconds):
        return None

    moments = iter((0.0, 10.0))
    monkeypatch.setattr(workflow_runner, "execute_tool_call", fake_execute)
    monkeypatch.setattr(workflow_runner.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(workflow_runner, "_monotonic", lambda: next(moments, 10.0))
    monkeypatch.setattr("app.agents.skills.executor.get_capabilities_for_scene", fake_scene)
    monkeypatch.setattr("app.agents.skills.executor.get_desktop_mcp_capabilities", fake_desktop)
    monkeypatch.setattr(workflow_runner.settings, "MCP_CLIENT_APPROVAL_WAIT_S", 5.0)

    result = asyncio.run(workflow_runner.run_workflow_skill(
        _OneCommitWorkflow(),
        {},
        SkillContext(user_id="u1", scene="office", conversation_id="job-1", job_id="job-1", workspace_id="ws-1"),
        workspace_id="ws-1",
    ))

    assert result.status == "cancelled"
    assert result.error_code == "CLIENT_APPROVAL_TIMEOUT"
    assert result.error_code != "NEEDS_CONFIRMATION"
