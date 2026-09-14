"""能力租约可见性回归（"模型识别不到读工具"的真因）。

线上现象：客户端明明注册了 ``workspace.read@1``、同一次任务的读取步骤也真的成功了，
界面却一直显示"缺少必需能力：workspace.read@1 · 没有可用的 Provider"。

真因有两个，本文件各自锁一条：

1. **单例撕裂**：``/capabilities/register`` 写的是
   ``app.services.capability_lease.capability_lease_service``，而 ``capability_broker``
   默认自建了另一个 ``CapabilityLeaseService`` —— 注册成功的租约对 Broker 永远不可见，
   于是提交期的 ``capability_resolution`` 恒判"没有可用的 Provider"。
2. **快照不可刷新**：``capability_resolution`` 只在提交时写一次，客户端随后连上也不会
   改口，前端就会把过期结论当现状一直显示。现在读取路径可以按
   ``capability_resolution_query`` 重算。
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
def lease_scope():
    """每个用例用**唯一主体**注册租约，用完摘干净（单例是进程级全局）。"""
    from app.services.capability_lease import capability_lease_service

    scope = {
        "user_id": f"u-{uuid.uuid4().hex[:8]}",
        "conversation_id": f"c-{uuid.uuid4().hex[:8]}",
        "workspace_id": f"w-{uuid.uuid4().hex[:8]}",
    }
    snapshot = dict(capability_lease_service._leases)
    try:
        yield scope
    finally:
        capability_lease_service._leases.clear()
        capability_lease_service._leases.update(snapshot)


def test_shared_broker_uses_the_registration_lease_service():
    """Broker 必须复用注册端点的那个租约服务实例，否则两端各有一套租约表。"""
    from app.agents.capabilities.broker.broker import capability_broker
    from app.services.capability_lease import capability_lease_service

    assert capability_broker.leases is capability_lease_service


@pytest.mark.asyncio
async def test_registered_lease_is_visible_to_broker_and_resolver(lease_scope):
    """注册后 Broker 必须能选到 Provider（修复前恒为 ``not_connected``）。"""
    from lumi_contracts.plugins import SessionBinding

    from app.agents.capabilities.broker.broker import capability_broker
    from app.agents.capabilities.registry.resolver import CapabilityResolver
    from app.services.capability_lease import capability_lease_service

    await capability_lease_service.register(
        provider_id="lumi.local.workspace",
        capabilities=[{"capability": "workspace.read", "contract_version": 1}],
        user_id=lease_scope["user_id"],
        device_id="device-1",
        conversation_id=lease_scope["conversation_id"],
        workspace_id=lease_scope["workspace_id"],
        deployment="client",
    )
    binding = SessionBinding(**lease_scope)

    selection = capability_broker.select(
        "workspace.read", contract_version=1, binding=binding
    )
    assert selection.provider_id == "lumi.local.workspace", (
        "注册端点与 Broker 必须是同一个租约视图，否则界面会谎报缺少必需能力"
    )
    assert selection.lease_state == ""

    report = CapabilityResolver(broker=capability_broker).resolve(
        ["workspace.read@1"], binding=binding
    )
    assert report.ok is True, report.as_dict()
    assert report.missing == []

    # 预检探测读的是同一份事实：注册后不得再报 provider_not_connected
    facts = capability_broker.preflight_facts(["workspace.read@1"], binding=binding)
    assert facts == {}


@pytest.mark.asyncio
async def test_missing_capability_resolution_carries_time_and_query(lease_scope):
    """提交期解析必须带时点戳与重算输入；重算后结论随时间变化。"""
    from lumi_contracts.plugins import SessionBinding

    from app.agents.capabilities.broker.broker import capability_broker
    from app.agents.capabilities.registry.resolver import CapabilityResolver
    from app.agents.orchestration.preflight.capability_preflight_service import (
        CAPABILITY_RESOLUTION_AT_KEY,
        CAPABILITY_RESOLUTION_QUERY_KEY,
        attach_capability_resolution_time,
        refresh_capability_resolution,
    )
    from app.services.capability_lease import capability_lease_service

    binding = SessionBinding(**lease_scope)
    before = CapabilityResolver(broker=capability_broker).resolve(
        ["workspace.read@1"], binding=binding
    )
    assert before.ok is False, "还没有 Provider 时必须是缺能力的真实结论"

    routing = {
        "capability_resolution": attach_capability_resolution_time(before.as_dict()),
        CAPABILITY_RESOLUTION_QUERY_KEY: {"required": ["workspace.read@1"], **lease_scope},
    }
    assert routing["capability_resolution"][CAPABILITY_RESOLUTION_AT_KEY]

    # 客户端随后连上并注册 → 读取路径刷新后不得再报缺能力
    await capability_lease_service.register(
        provider_id="lumi.local.workspace",
        capabilities=[{"capability": "workspace.read", "contract_version": 1}],
        user_id=lease_scope["user_id"],
        device_id="device-1",
        conversation_id=lease_scope["conversation_id"],
        workspace_id=lease_scope["workspace_id"],
        deployment="client",
    )
    await refresh_capability_resolution(routing)
    assert routing["capability_resolution"]["ok"] is True
    assert routing["capability_resolution"]["missing"] == []


@pytest.mark.asyncio
async def test_refresh_keeps_old_snapshot_when_it_cannot_recompute(monkeypatch):
    """刷新失败必须保留旧快照并留下错误痕迹，绝不把任务详情打挂。"""
    from app.agents.orchestration.preflight.capability_preflight_service import (
        CAPABILITY_RESOLUTION_QUERY_KEY,
        refresh_capability_resolution,
    )

    routing = {
        "capability_resolution": {"ok": False, "missing": [{"capability": "workspace.read@1"}]},
        CAPABILITY_RESOLUTION_QUERY_KEY: {"required": ["workspace.read@1"], "user_id": "u1"},
    }

    def _boom(*_args, **_kwargs):
        raise RuntimeError("broker 不可用")

    monkeypatch.setattr(
        "app.agents.capabilities.registry.resolver.CapabilityResolver.resolve", _boom
    )
    await refresh_capability_resolution(routing)
    assert routing["capability_resolution"]["ok"] is False, "失败时保留旧快照"
    assert "broker 不可用" in routing["capability_resolution_error"]


@pytest.mark.asyncio
async def test_tool_call_capability_gate_is_not_skipped_by_a_none_lease_service(
    lease_scope, monkeypatch
):
    """工具调用时能力门禁必须真的跑起来，而不是因为 ``lease_service=None`` 抛异常被跳过。

    修复前：``execute_tool_call`` 的缺省 ``capability_lease_service=None`` 一路传到
    ``CapabilityDispatchAdapter``，在 ``self._leases.snapshot()`` 抛 ``AttributeError``，
    被 ``try_capability_route`` 吞掉后**每次工具调用**都打
    "路由门禁异常（落回旧路径）… 'NoneType' object has no attribute 'snapshot'"。
    """
    from app.agents.skills import capability_route

    logged: list[str] = []

    class _Sink:
        def warning(self, message, *args, **kwargs):  # noqa: ANN002, ANN003
            logged.append(str(message).format(*args) if args else str(message))

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(capability_route, "logger", _Sink())
    monkeypatch.setattr(capability_route, "routing_mode", lambda: "active")

    result = await capability_route.try_capability_route(
        tool_name="mcp__lumi_client__workspace_navigator",
        args={"action": "list"},
        user_id=lease_scope["user_id"],
        conversation_id=lease_scope["conversation_id"],
        workspace_id=lease_scope["workspace_id"],
        lease_service=None,
    )
    assert logged == [], f"门禁不得因租约服务缺失而异常落回旧路径：{logged}"
    assert result is None, "只读能力无租约时按契约回退旧路径（不是接管）"


def test_capability_route_resolves_the_shared_lease_service_when_not_injected():
    """缺省注入必须解析到共享租约服务（与注册端点同一个实例）。"""
    from app.agents.skills.capability_route import _shared_lease_service
    from app.services.capability_lease import capability_lease_service

    assert _shared_lease_service() is capability_lease_service
