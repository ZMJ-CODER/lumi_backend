"""executor 能力路由门禁的端到端回归（真跑 ``execute_tool_call``）。

覆盖三件事：

1. ``off`` 模式**行为与旧版逐字相同**（走原 MCP/聚合分支，不查租约）；
2. ``read_only`` 模式在**有租约**时真的把工具调用转成按 Provider 的 MCP 调用，
   并把 ``route={provider_id, lease_id, plugin_id}`` 传下去；
3. ``read_only`` 模式在没有租约时**落回旧路径**（只读可回退），而 ``active`` 模式下
   写能力没有租约时**结构化失败**（不回退）。

这里不 mock 能力路由本身，只 mock 边界：工具能力查询、MCP manager、租约服务。
"""

from __future__ import annotations

import asyncio

import pytest

from lumi_contracts.plugins import Deployment, ProviderHealth, ProviderLease

from app.agents.skills import executor as ex
from app.services.capability_lease_redis import lease_id_for

WORKSPACE = "ws-trusted"
USER = "u1"
CONV = "conv-1"


@pytest.fixture(autouse=True)
def _available_effect_journal(monkeypatch):
    """隔离副作用安全日志（与 ``test_process_event_semantics`` 同一保护）。

    写类步骤会先问副作用安全日志；模块级 ``effects._repository`` 是**全局**状态，
    一旦本文件把 ``PostgresEffectJournalRepository`` 惰性建起来（无数据库连接），
    后续用例里的写步骤就会以"副作用安全日志不可用"失败——这是跨文件污染，
    必须在这里显式装回内存实现（monkeypatch 结束自动还原）。
    """
    from app.agents.orchestration.runtime import effects
    from app.repositories.effect_journal_repository import InMemoryEffectJournalRepository

    monkeypatch.setattr(effects, "_repository", InMemoryEffectJournalRepository())


def _capability(raw_name: str, *, server: str = "lumi_pc"):
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


def _lease(*, capability: str = "workspace.read", **overrides) -> ProviderLease:
    payload = {
        "provider_id": "lumi.local.workspace",
        "capability": capability,
        "contract_version": 1,
        "lease_id": lease_id_for("lumi.local.workspace", capability),
        "user_id": USER,
        "device_id": "device-1",
        "workspace_id": WORKSPACE,
        "conversation_id": CONV,
        "deployment": Deployment.CLIENT,
        "plugin_id": "lumi.local.workspace",
        "health_status": ProviderHealth.HEALTHY.value,
        "expires_at": 9_999_999_999.0,
    }
    payload.update(overrides)
    return ProviderLease(**payload)


class _FakeLeaseService:
    def __init__(self, leases: list[ProviderLease]) -> None:
        self._leases = leases
        self.refreshed = 0

    def snapshot(self, *, purge: bool = True) -> list[ProviderLease]:
        return list(self._leases)

    async def refresh_from_redis(self) -> list[ProviderLease]:
        self.refreshed += 1
        return list(self._leases)


def _install(monkeypatch, *, raw_name: str, mcp_payload: dict | None = None):
    """装好能力查询 + MCP manager 替身，返回记录容器。"""
    captured: dict = {"mcp_calls": []}

    async def fake_capability(name, scene, user_role="user", user_id="", **kwargs):
        return _capability(raw_name)

    monkeypatch.setattr(ex, "get_tool_capability", fake_capability)

    async def fake_call_tool(name, tool_name, args=None, **kwargs):
        captured["mcp_calls"].append({"name": name, "tool": tool_name, "args": dict(args or {}), **kwargs})
        return mcp_payload or {
            "status": "ok",
            "content": "读取完成",
            "data": {"entries": [{"name": "a.txt"}]},
        }

    monkeypatch.setattr("app.agents.mcp.manager.call_tool", fake_call_tool)
    return captured


def _call(name: str, arguments: dict, *, lease_service, **kwargs):
    return asyncio.run(
        ex.execute_tool_call(
            {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}},
            USER,
            "office",
            CONV,
            authorized_workspace_id=WORKSPACE,
            capability_lease_service=lease_service,
            **kwargs,
        )
    )


def test_off_mode_never_touches_lease_service(monkeypatch):
    """off（默认）：连租约都不查，行为与旧版一致。"""
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "off")
    leases = _FakeLeaseService([_lease()])
    captured = _install(monkeypatch, raw_name="workspace_search")
    result = _call("mcp__lumi_pc__workspace_search", {"query": "x"}, lease_service=leases)
    assert leases.refreshed == 0, "off 模式必须零开销"
    assert captured["mcp_calls"] == [], "off 模式不得经能力层派发"
    assert result.status in {"failed", "success", "partial", "pending_approval"}


def test_read_only_mode_dispatches_with_lease_route(monkeypatch):
    """read_only：有租约时按 Provider 派发，且 route 一路带到 MCP。"""
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "read_only")
    leases = _FakeLeaseService([_lease()])
    captured = _install(monkeypatch, raw_name="workspace_navigator")
    result = _call(
        "mcp__lumi_pc__workspace_navigator",
        {"action": "read", "path": "a.txt"},
        lease_service=leases,
    )
    assert leases.refreshed == 1, "派发前必须同步权威租约（跨 worker）"
    assert len(captured["mcp_calls"]) == 1
    call = captured["mcp_calls"][0]
    assert call["name"] == "lumi.local.workspace"
    assert call["route"] == {
        "provider_id": "lumi.local.workspace",
        "plugin_id": "lumi.local.workspace",
        "lease_id": lease_id_for("lumi.local.workspace", "workspace.read"),
        "capability": "workspace.read@1",
        # 执行来源随路由下发：前端/审计据此显示"谁在执行"，不再靠 deployment 猜。
        "execution_plane": "client",
        "runtime_kind": "in_process",
        "executor_type": "client",
    }
    assert call["workspace_id"] == WORKSPACE
    assert result.success is True
    assert result.metadata["capability_routed"] is True
    assert result.metadata["provider_id"] == "lumi.local.workspace"
    assert result.metadata["served_locally"] is True


def test_read_only_mode_falls_back_when_no_lease(monkeypatch):
    """只读能力没有租约：不接管（落回旧路径），调用方仍拿到正常结果。"""
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "read_only")
    leases = _FakeLeaseService([])
    _install(
        monkeypatch,
        raw_name="workspace_search",
        mcp_payload={"status": "ok", "content": "旧路径结果", "data": {}},
    )
    result = _call("mcp__lumi_pc__workspace_search", {"query": "x"}, lease_service=leases)
    # 关键：能力层**没有**接管（没有 capability_routed 标记）。
    # 旧路径最终是走 MCP 还是聚合服务由既有逻辑决定，两者都算"未被接管"。
    assert not result.metadata.get("capability_routed")
    assert leases.refreshed == 1, "只读模式下会查询一次租约以判断能否接管"


def test_active_mode_blocks_write_without_approval(monkeypatch):
    """active：写能力既没审批也没租约 → 结构化失败（`APPROVAL_REQUIRED`），绝不静默回退。

    审批门禁在选 Provider **之前**：用户需要的是"去审批"，而不是"去装 Provider"。
    """
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "active")
    leases = _FakeLeaseService([])
    captured = _install(monkeypatch, raw_name="workspace_stage_write")
    result = _call(
        "mcp__lumi_pc__workspace_stage_write",
        {"path": "a.txt", "content": "x"},
        lease_service=leases,
    )
    assert result.success is False
    assert result.error_code == "APPROVAL_REQUIRED"
    assert result.retryable is True
    assert result.metadata["capability_routed"] is True
    assert all(not call.get("route") for call in captured["mcp_calls"]), "未审批时不得派发"


def test_approved_write_call_routes_with_lease(monkeypatch):
    """带**确切指纹**的工具级审批 + 有租约 → 写能力正常按租约派发。"""
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "active")
    from app.agents.skills.executor import tool_call_fingerprint

    args = {"path": "a.txt", "content": "x"}
    leases = _FakeLeaseService([_lease(capability="workspace.write")])
    captured = _install(monkeypatch, raw_name="workspace_stage_write")
    result = _call(
        "mcp__lumi_pc__workspace_stage_write",
        args,
        lease_service=leases,
        confirmed_tool_calls=frozenset(
            {tool_call_fingerprint("mcp__lumi_pc__workspace_stage_write", args)}
        ),
    )
    assert result.success is True
    assert len(captured["mcp_calls"]) == 1
    assert captured["mcp_calls"][0]["route"]["capability"] == "workspace.write@1"


def test_stale_fingerprint_does_not_authorize_different_arguments(monkeypatch):
    """批准的是 A、实际调 B：指纹不符 → 不派发（不得靠"工具在已批准列表里"蒙混）。"""
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "active")
    from app.agents.skills.executor import tool_call_fingerprint

    approved_args = {"path": "a.txt", "content": "old"}
    leases = _FakeLeaseService([_lease(capability="workspace.write")])
    captured = _install(monkeypatch, raw_name="workspace_stage_write")
    result = _call(
        "mcp__lumi_pc__workspace_stage_write",
        {"path": "a.txt", "content": "TAMPERED"},
        lease_service=leases,
        confirmed_tool_calls=frozenset(
            {tool_call_fingerprint("mcp__lumi_pc__workspace_stage_write", approved_args)}
        ),
    )
    assert result.success is False
    assert result.error_code == "APPROVAL_REQUIRED"
    assert captured["mcp_calls"] == []


def test_shadow_mode_keeps_legacy_path(monkeypatch):
    """shadow：只打点，执行路径不变（旧分支仍然跑）。"""
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "shadow")
    leases = _FakeLeaseService([_lease()])
    captured = _install(
        monkeypatch, raw_name="workspace_search", mcp_payload={"status": "ok", "content": "旧路径", "data": {}}
    )
    result = _call("mcp__lumi_pc__workspace_search", {"query": "x"}, lease_service=leases)
    assert leases.refreshed == 1, "shadow 会查询（打点需要）"
    assert captured["mcp_calls"], "shadow 必须继续走旧路径"
    assert not result.metadata.get("capability_routed"), "shadow 不得标记为已派发"


def test_local_action_tools_bypass_capability_route(monkeypatch):
    """本机动作（打开应用/澄清）不参与租约，即使 active 也不接管。"""
    monkeypatch.setattr("app.core.config.settings.AGENT_CAPABILITY_ROUTING_MODE", "active")
    leases = _FakeLeaseService([_lease()])
    _install(monkeypatch, raw_name="workspace_read")
    result = _call("mcp__lumi_pc__desktop_open_app", {"name": "notepad"}, lease_service=leases)
    assert leases.refreshed == 0
    assert not result.metadata.get("capability_routed")


def test_qualified_mcp_names_are_mapped_to_capabilities():
    """真实调用用的是限定名（``mcp__server__tool``），路由表必须认它。"""
    from app.agents.capabilities.registry.builtin import capability_for_tool

    assert capability_for_tool("mcp__lumi_pc__workspace_navigator") == "workspace.read"
    assert capability_for_tool("mcp__lumi_pc__workspace_stage_write") == "workspace.write"
    assert capability_for_tool("mcp__lumi_pc__sandbox_run") == "code.execute"
    assert capability_for_tool("mcp__lumi_pc__workspace_commit") == "git.operations"
    assert capability_for_tool("mcp__lumi_pc__desktop_open_app") is None
    # 未知工具不猜
    assert capability_for_tool("mcp__lumi_pc__totally_unknown") is None
