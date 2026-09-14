"""D 项回归：ToolRequest 边界 + ExecutionRequest 下传。

* 工具调用入口构造契约 ``ToolRequest``：参数必须是对象、关联标识服务端注入；
* ``call_id`` 必须跨 MCP 跳保留（以前在这一层丢失，无法对账同一次调用）；
* ``ExecutionRequest`` 作为 TaskProfile → RouteDecision 之后的执行请求进入 Job 快照
  （只存授权事实与指令摘要，不重复存用户原文）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from lumi_contracts import RouteDecision, RouteMode, Sensitivity, ServerContext

from app.contracts.routing import execution_request, execution_request_snapshot


@pytest.fixture()
def _echo_tool(monkeypatch):
    """注册一个极简工具，并绕过能力表（能力表来自策略白名单）。

    这里只验证**执行器入口的契约行为**（ToolRequest 构造 / call_id 保留 /
    参数必须是对象），因此能力解析被替换成确定性桩。
    """
    from types import SimpleNamespace

    import app.agents.skills.executor as executor
    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    class _EchoTool(Tool):
        name = "demo_echo"
        description = "回显参数"
        parameters_schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

        async def execute(self, params, context=None):
            return ToolOutput(status="success", output=str(params.get("text") or ""), data=dict(params))

    ToolRegistry.register(_EchoTool(), source="test")

    async def fake_capability(name, *_args, **_kwargs):
        return SimpleNamespace(
            name=name,
            description="回显参数",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            requires_confirmation=False,
            environment="server",
            version="1.0.0",
            category="general",
            domain="",
            annotations={},
            intent_tags=[],
            conflicts_with=[],
            preferred_over=[],
            use_when=[],
            do_not_use_when=[],
            bootstrap_intents=[],
            bootstrap_until="",
            confirmation_mode="",
        )

    monkeypatch.setattr(executor, "get_tool_capability", fake_capability)
    yield "demo_echo"
    ToolRegistry.unregister("demo_echo")


def _call(name: str, arguments: Any, **kwargs):
    from app.agents.skills.executor import execute_tool_call

    tool_call = {
        "id": kwargs.pop("tool_call_id", "call-abc"),
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }
    return asyncio.run(execute_tool_call(tool_call, "u1", "chat", "c1", **kwargs))


def test_tool_request_preserves_call_id_across_the_mcp_hop(_echo_tool):
    result = _call(_echo_tool, {"text": "hi"}, mcp_call_id="call-xyz")
    assert result.status == "success"
    # call_id 必须原样保留（这一层以前会丢）
    assert result.call_id == "call-xyz"


def test_non_object_arguments_return_a_fixable_error(_echo_tool):
    result = _call(_echo_tool, ["not", "an", "object"])
    assert result.status == "failed"
    assert result.error_code == "INVALID_ARGS"
    assert "对象" in str(result.error)


def test_execution_request_snapshot_is_auditable_without_duplicating_text():
    context = ServerContext(
        user_id="u1", user_role="user", conversation_id="c1", workspace_id="ws-1",
        data_sensitivity=Sensitivity.CONFIDENTIAL,
    )
    request = execution_request(
        "读一下 README 并总结",
        context=context,
        route=RouteDecision(mode=RouteMode.ATOMIC_READ),
        allowed_tools=("workspace_navigator",),
        denied_tools=("sandbox_run",),
        max_steps=3,
    )
    snapshot = execution_request_snapshot(request)
    assert snapshot["schema_name"] == "lumi.execution_request"
    assert snapshot["schema_version"] == 1
    assert snapshot["route_mode"] == "m1_atomic_read"
    assert snapshot["allowed_tools"] == ["workspace_navigator"]
    assert snapshot["denied_tools"] == ["sandbox_run"]
    assert snapshot["max_steps"] == 3
    assert snapshot["data_sensitivity"] == "CONFIDENTIAL"
    assert snapshot["workspace_bound"] is True
    assert snapshot["conversation_bound"] is True
    assert snapshot["instruction_chars"] == len("读一下 README 并总结")
    # 不重复存用户原文
    assert "读一下 README" not in json.dumps(snapshot, ensure_ascii=False)
    assert isinstance(snapshot["instruction_sha256"], str)


def test_execution_request_is_injected_into_job_routing(monkeypatch):
    """链路末端：Job 快照里能看到这次执行的授权事实。"""
    from app.agents.orchestration.orchestrator import AgentOrchestrator
    from app.agents.orchestration.planning.planner import Planner, TaskTree
    from app.agents.orchestration.execution.review import NoopReviewer
    from app.agents.orchestration.runtime.state import InMemoryStateStore
    from app.agents.orchestration.models import TaskNode
    from app.core.config import settings

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", True)

    class _P(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[TaskNode(id="s1", name="s1", agent="w1")], plan_text="t")

        async def plan_for_level(self, *args, **kwargs):
            return TaskTree(nodes=[TaskNode(id="s1", name="s1", agent="w1")], plan_text="t")

    class _W:
        async def execute(self, node, ctx):
            return {"success": True, "content": "x"}

    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=_P(),
        workers={"w1": _W()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        job = await orch.submit_job("u1", "读一下 README 并总结", conversation_id="c1", workspace_id="ws-1")
        await asyncio.gather(*orch._tasks.values())
        return await orch.get_job(job.job_id)

    final = asyncio.run(scenario())
    snapshot = final.routing["execution_request"]
    assert snapshot["schema_name"] == "lumi.execution_request"
    assert snapshot["route_mode"] == "m1_atomic_read"
    assert snapshot["workspace_bound"] is True
    assert "读一下 README 并总结" not in json.dumps(snapshot, ensure_ascii=False)
