"""``workspace_navigator`` 后端路由与工具注册回归测试。

覆盖联调协议第 1、2、3 条：

1. 工具发现：发给模型的读取工具只有一个 workspace_navigator，内部原子名不出现；
2. 工具调用：user_id / conversation_id / workspace_id / device_id / server_name
   由后端注入，模型只传 action/path/query/depth/cursor；
3. 返回结果：统一信封 + 稳定错误码，错误能指导模型自我修正。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.skills import executor as ex
from app.workspace.read import navigator as wn


@pytest.fixture(autouse=True)
def _reset_cursors():
    wn.CURSOR_STORE.clear()
    yield
    wn.CURSOR_STORE.clear()


def _capability(raw_name: str = "workspace_navigator", server: str = "lumi_pc"):
    from app.agents.skills.capability import ToolCapability

    return ToolCapability(
        name=f"mcp__{server}__{raw_name}",
        version="1.0.0",
        status="stable",
        description="",
        category="workspace",
        domain="workspace",
        parameters={"type": "object", "properties": {}},
        source="mcp",
        environment="client",
        server=server,
        raw_name=raw_name,
        permission="user",
        write_op=False,
        requires_confirmation=False,
        confirmation_mode="client",
        idempotent=True,
        annotations={"provider": "desktop_mcp"},
    )


def _install(monkeypatch, payload: dict, *, raw_name: str = "workspace_navigator"):
    captured: dict = {}

    async def fake_capability(name, scene, user_role="user", user_id="", **kwargs):
        return _capability(raw_name, server=name.split("__")[1] if "__" in name else "lumi_pc")

    monkeypatch.setattr(ex, "get_tool_capability", fake_capability)

    class FakeNavigator:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        async def execute(self, action, params):
            captured["action"] = action
            captured["params"] = dict(params or {})
            return payload

    monkeypatch.setattr(wn, "WorkspaceNavigatorService", FakeNavigator)
    return captured


def _call(name: str, arguments: dict, **kwargs):
    return asyncio.run(ex.execute_tool_call(
        {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}},
        "u1",
        "office",
        "conv-1",
        authorized_workspace_id="ws-trusted",
        **kwargs,
    ))


def test_internal_read_names_are_not_routed_as_model_entry(monkeypatch):
    """内部原子名不再走聚合入口（避免模型绕过统一读取协议）。"""
    captured = _install(monkeypatch, {"status": "ok", "action": "read"})
    result = _call("mcp__lumi_pc__workspace_search", {"query": "x"})
    assert "action" not in captured  # 未被聚合服务接管
    assert result.status in {"failed", "success", "partial", "pending_approval"}


def test_navigator_action_is_validated_before_dispatch(monkeypatch):
    captured = _install(monkeypatch, {"status": "ok", "action": "list"})
    result = _call("mcp__lumi_pc__workspace_navigator", {"action": "delete"})
    assert result.success is False
    assert result.error_code == "INVALID_ACTION"
    assert "action" not in captured
    assert "list" in str(result.error)


def test_navigator_injects_trusted_workspace_and_drops_model_value(monkeypatch):
    captured = _install(monkeypatch, {
        "status": "ok", "action": "list", "summary": "根目录 1 个条目",
        "data": {"entries": [{"name": "a.txt", "path": "a.txt", "kind": "file"}]},
        "has_more": False, "cursor": None, "meta": {}, "error": None,
    })
    result = _call(
        "mcp__lumi_pc__workspace_navigator",
        {"action": "list", "workspace_id": "attacker", "path": ""},
    )
    assert captured["init"]["workspace_id"] == "ws-trusted"
    assert captured["init"]["conversation_id"] == "conv-1"
    assert captured["init"]["user_id"] == "u1"
    assert captured["action"] == "list"
    assert result.status == "success"
    assert result.metadata["navigator_action"] == "list"
    assert result.metadata["result_count"] == 1


def test_navigator_failure_is_projected_with_stable_code_and_hint(monkeypatch):
    _install(monkeypatch, wn.build_error(
        wn.STATUS_ERROR, "read", wn.WORKSPACE_PATH_NOT_DIRECTORY,
        "docs 是目录，不能作为文件读取。",
        suggested_action="list",
    ))
    result = _call("mcp__lumi_pc__workspace_navigator", {"action": "read", "path": "docs"})
    assert result.success is False
    assert result.error_code == wn.WORKSPACE_PATH_NOT_DIRECTORY
    text = str(result.output)
    assert "WORKSPACE_PATH_NOT_DIRECTORY" in text
    assert "list" in text  # 可自我修正的下一步


def test_navigator_requires_workspace_scope(monkeypatch):
    _install(monkeypatch, {"status": "ok", "action": "list"})
    result = asyncio.run(ex.execute_tool_call(
        {"id": "c", "type": "function", "function": {
            "name": "mcp__lumi_pc__workspace_navigator",
            "arguments": {"action": "list"},
        }},
        "u1", "office", "conv-1",
        authorized_workspace_id="",
    ))
    assert result.success is False
    assert result.error_code == "WORKSPACE_SCOPE_REQUIRED"


def test_navigator_payload_stays_structured_for_orchestrator(monkeypatch):
    payload = {
        "status": "partial", "action": "read", "summary": "已读取 a.pptx（1/3 段）",
        "data": {"path": "a.pptx", "sections": [
            {"source": "a.pptx", "location": "slide-1", "title": "概述", "text": "正文"}
        ]},
        "has_more": True, "cursor": "nav-cursor", "meta": {"format": "pptx"}, "error": None,
    }
    _install(monkeypatch, payload)
    result = _call("mcp__lumi_pc__workspace_navigator", {"action": "read", "path": "a.pptx"})
    assert result.status == "success"
    assert result.data == payload  # 编排层仍拿到完整信封
    assert json.loads(json.dumps(result.data, ensure_ascii=False)) == payload
    assert "nav-cursor" in str(result.output)


def test_desktop_alias_maps_read_action_to_atomic_read():
    from app.agents.mcp.manager import _desktop_workspace_alias

    mapped = _desktop_workspace_alias(
        "workspace_navigator", {"action": "read", "path": "a.txt", "workspace_id": "w1"}, task_id="t1"
    )
    assert mapped is not None
    tool, args = mapped
    assert tool == "workspace_read" and args["path"] == "a.txt"
    # list/search 不映射到单个原子工具（需要客户端升级），避免语义错位。
    assert _desktop_workspace_alias(
        "workspace_navigator", {"action": "list", "workspace_id": "w1"}, task_id="t1"
    ) is None


def test_aggregated_dependency_is_synthesized_from_atomic_tools():
    from app.agents.skills.dependencies import resolve_dependencies, synthesize_aggregated_capabilities

    caps = {
        "mcp__lumi_pc__workspace_read": {
            "version": "1.0.0", "provider": "desktop_mcp", "environment": "client",
            "annotations": {"availability_hint": "available"},
        }
    }
    merged = {**caps, **synthesize_aggregated_capabilities(caps)}
    report = resolve_dependencies(
        {"tools": [{"name": "mcp__lumi_pc__workspace_navigator", "min_version": "1.0.0", "provider": "desktop_mcp"}]},
        merged,
        execution_scope="backend_orchestrates_client",
    )
    assert report.required_issues == []


def test_aggregated_dependency_missing_when_no_atomic_tool():
    from app.agents.skills.dependencies import resolve_dependencies, synthesize_aggregated_capabilities

    merged = synthesize_aggregated_capabilities({})
    report = resolve_dependencies(
        {"tools": [{"name": "mcp__lumi_pc__workspace_navigator", "provider": "desktop_mcp"}]},
        merged,
        execution_scope="backend_orchestrates_client",
    )
    assert [item.code for item in report.required_issues] == ["MISSING_TOOL"]
