"""MCP 工具收敛回归：不重复设计输入 Schema / 不套 ToolRequest 外壳。

* MCP 工具的输入定义以 **MCP 的 inputSchema 为唯一事实来源**：契约里只留引用
  （``input_schema_ref = mcp://<server>/<tool>``），需要参数表单/校验时动态拉取；
* MCP 调用**直接走 MCP client**，不再包一层 ``ToolRequest``；
* 进程内工具仍保留 ``ToolRequest``（参数对象校验 + 关联标识 + 幂等/审批绑定）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from lumi_contracts import ToolSpec

from app.contracts.tools import (
    mcp_input_schema,
    split_mcp_schema_ref,
    tool_spec_from,
)


def _mcp_capability() -> SimpleNamespace:
    return SimpleNamespace(
        name="workspace_read",
        raw_name="workspace_read",
        server="lumi_client",
        source="mcp",
        environment="client",
        version="1.0.0",
        description="读取工作区文件",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        write_op=False,
        requires_confirmation=False,
        permission="user",
        category="filesystem",
    )


def test_mcp_tool_spec_does_not_copy_the_input_schema():
    spec = tool_spec_from(_mcp_capability())
    assert isinstance(spec, ToolSpec)
    # 不再复制一份 Schema（MCP 才是权威定义）
    assert spec.input_schema == {}
    assert spec.input_schema_ref == "mcp://lumi_client/workspace_read"
    assert split_mcp_schema_ref(spec.input_schema_ref) == ("lumi_client", "workspace_read")
    # 声明仍然合法：有引用时不再要求本地 object schema
    assert spec.validate_declaration() == []
    # 客户端/MCP 能力单独命名空间
    assert spec.namespace == "lumi_client"


def test_local_tool_still_declares_its_own_schema():
    local = SimpleNamespace(
        name="demo_local",
        source="plugin",
        environment="server",
        version="1.0.0",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        write_op=False,
    )
    spec = tool_spec_from(local)
    assert spec.input_schema == {"type": "object", "properties": {"x": {"type": "integer"}}}
    assert spec.input_schema_ref == ""


def test_mcp_input_schema_is_pulled_from_the_mcp_client(monkeypatch):
    import app.agents.mcp.manager as manager

    async def fake_list_tools(server: str):
        assert server == "lumi_client"
        return [
            {"name": "workspace_read", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        ]

    monkeypatch.setattr(manager, "list_tools", fake_list_tools)
    schema = asyncio.run(mcp_input_schema("lumi_client", "workspace_read"))
    assert schema["properties"]["path"]["type"] == "string"
    # 拉取失败/未知工具都退化为空（调用方按无约束对象处理），不抛异常
    assert asyncio.run(mcp_input_schema("lumi_client", "nope")) == {}
    assert asyncio.run(mcp_input_schema("", "")) == {}


def test_mcp_call_does_not_wrap_arguments_in_tool_request(monkeypatch):
    """MCP 分支必须直接把参数交给 MCP client，不再构造 ToolRequest。"""
    import app.agents.skills.executor as executor

    captured: dict = {}

    async def fake_call_tool(server, tool, args, **kwargs):
        captured["server"] = server
        captured["tool"] = tool
        captured["args"] = dict(args)
        captured["call_id"] = kwargs.get("call_id")
        return {
            "status": "success",
            "data": {"path": args.get("path")},
            "content_type": "structured",
            "call_id": kwargs.get("call_id"),
        }

    monkeypatch.setattr("app.agents.mcp.manager.call_tool", fake_call_tool)
    monkeypatch.setattr(executor, "_claim_tool_execution", _noop_claim)
    # MCP 能力由桌面端发布后在能力表里可见；这里给确定性桩。
    monkeypatch.setattr(executor, "get_tool_capability", _fake_capability)

    tool_call = {
        "id": "call-mcp",
        "type": "function",
        "function": {"name": "mcp__lumi_client__read_file", "arguments": json.dumps({"path": "a.txt"})},
    }
    result = asyncio.run(
        executor.execute_tool_call(tool_call, "u1", "office", "c1", mcp_call_id="call-mcp")
    )
    assert captured["server"] == "lumi_client"
    assert captured["tool"] == "read_file"
    # 参数原样交给 MCP client（不经过任何本地输入适配/重命名）
    assert captured["args"] == {"path": "a.txt"}
    assert captured["call_id"] == "call-mcp"
    assert result.status == "success"


class _noop_claim:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


async def _fake_capability(name, *_args, **_kwargs):
    return SimpleNamespace(
        name=name,
        description="MCP 能力",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        requires_confirmation=False,
        environment="client",
        version="1.0.0",
        category="filesystem",
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


@pytest.mark.parametrize("ref", ["", "lumi.workspace.result", "http://x/y"])
def test_non_mcp_refs_are_ignored(ref):
    assert split_mcp_schema_ref(ref) == ("", "")
