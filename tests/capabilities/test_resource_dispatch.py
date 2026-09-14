"""统一资源能力层 Phase 3（统一 Broker 派发）回归。

Phase 3 的主张：**派发拿到的是结构化目标，不是从工具名猜出来的能力**。

```text
capability（兼容名，Broker/租约/目录的键） + unified_capability + resource_type
        ↓ Provider Adapter（能力 ↔ 底层 MCP 原子工具的唯一转换点）
provider_id（不是按名字猜的） + mcp_target
```

三层断言：

1. **结构化解析**：工具名 → 目标（能力/资源类型/Provider/底层工具），不认识就不猜；
2. **Provider Adapter**：能力+资源类型 → Provider 与底层工具名，新 Provider 不必改调用点；
3. **收窄与兼容**：Broker 按资源类型收窄候选，但**收窄后无候选时按收窄前继续**；
   关闭开关时旧解析逐字不变（`capability_for_mcp_tool`）。
"""

from __future__ import annotations

import time

import pytest

from app.agents.capabilities.broker import resource_dispatch as rd


@pytest.fixture()
def dispatch_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_DISPATCH", True)
    return settings


@pytest.fixture()
def dispatch_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_DISPATCH", False)
    return settings


# ── 1. 结构化派发目标 ───────────────────────────────────────


def test_flag_defaults_to_off():
    from app.core.config import settings

    assert settings.RESOURCE_CAPABILITY_DISPATCH is False
    assert rd.dispatch_enabled() is False


def test_resolve_dispatch_returns_structured_target():
    target = rd.resolve_dispatch("mcp__lumi_client__workspace_write")
    assert target.capability == "workspace.write"          # Broker/租约的键（兼容名）
    assert target.unified_capability == "resource.write"   # 统一能力
    assert target.resource_type == "workspace"
    assert target.provider_name == "workspace_provider"
    assert target.provider_id == "lumi.local.workspace"
    assert target.mcp_target == "workspace_write"
    assert target.source == "resource"
    assert target.known is True


def test_resolve_dispatch_never_guesses():
    for tool in ("totally_unknown_tool", "super_writer", ""):
        target = rd.resolve_dispatch(tool)
        assert target.known is False, tool
        assert target.unified_capability == ""
        assert target.source in {"unknown", "static"}, tool


def test_resolve_dispatch_keeps_legacy_capability_for_compat():
    """旧 MCP 依赖给的能力名照旧能派发（兼容解析），但不编统一能力。

    * 完全未知的工具 → 不编能力、不编统一能力（``unknown``）；
    * 显式给出旧能力名（旧 Workflow 依赖走这条）→ **能**映射到统一能力，
      这正是"旧 MCP 依赖保留一个版本周期"的落点。
    """
    target = rd.resolve_dispatch("desktop_open_url")
    assert target.known is False
    assert target.source == "unknown"
    hinted = rd.resolve_dispatch("legacy_tool", capability_hint="workspace.write")
    assert hinted.capability == "workspace.write"
    assert hinted.unified_capability == "resource.write"
    assert hinted.resource_type == "workspace"
    assert hinted.known is True


def test_resolve_dispatch_honours_declared_capability():
    """调用方给出的能力/资源类型优先；**歧义不猜**。

    ``resource.write`` 这种统一能力本身跨多种资源（workspace / office_document / …），
    只给能力不给资源类型时必须判为"不知道"，而不是挑一个资源。
    """
    legacy_hint = rd.resolve_dispatch("some_tool", capability_hint="workspace.write")
    assert legacy_hint.known is True
    assert legacy_hint.unified_capability == "resource.write"
    assert legacy_hint.resource_type == "workspace"

    ambiguous = rd.resolve_dispatch("some_tool", capability_hint="resource.write")
    assert ambiguous.known is False, "统一能力 + 无资源类型 = 歧义，不能猜"

    # 显式给出资源类型 → 明确（新 Provider 的新资源走这条）
    explicit = rd.resolve_dispatch(
        "memory_write", capability_hint="resource.write", resource_type="memory"
    )
    assert explicit.known is True
    assert explicit.unified_capability == "resource.write"
    assert explicit.resource_type == "memory"
    # 缺口 2a：memory 只有**声明**（registered=False）⇒ 不编 provider_id，
    # 但逻辑 Provider 名（声明）照样给出，管理端据此显示"待接入"。
    assert explicit.provider_id == ""
    assert explicit.provider_name == "memory_provider"
    unknown_resource = rd.resolve_dispatch(
        "custom_write", capability_hint="resource.write", resource_type="custom_store"
    )
    assert unknown_resource.known is True
    assert unknown_resource.provider_id == ""
    assert unknown_resource.provider_name == ""


def test_dispatch_target_is_json_safe():
    payload = rd.resolve_dispatch("workspace_write").as_dict()
    assert set(payload) == {
        "tool", "capability", "unified_capability", "resource_type",
        "provider", "provider_id", "mcp_target", "source", "known",
    }
    import json

    json.dumps(payload, ensure_ascii=False)


# ── 2. Provider Adapter ─────────────────────────────────────


def test_adapter_resolution_prefers_registered_provider():
    adapter = rd.adapter_for("resource.write", "workspace")
    assert adapter is not None
    assert adapter.name == "workspace_provider"
    assert adapter.provider_id == "lumi.local.workspace"
    assert adapter.supports("resource.write", "workspace") is True
    assert adapter.supports("resource.write", "office_document") is False
    # 只有声明、还没有实现的 Provider 不参与首选
    assert rd.adapter_for("resource.read", "knowledge") is None
    assert [item.name for item in rd.adapters_for("resource.read", "knowledge")] == [
        "knowledge_provider"
    ]


def test_adapter_maps_capability_to_mcp_tool():
    """Adapter 是"能力 → 底层工具名"的**唯一**转换点。"""
    adapter = rd.adapter_for("resource.read", "workspace")
    assert adapter is not None
    assert adapter.mcp_tool_for("resource.read", "workspace") == "workspace_navigator"
    assert rd.adapter_tool_for("resource.edit", "workspace") == "workspace_edit"
    assert rd.adapter_tool_for("artifact.create", "artifact") == "create_office_document"
    # 没有对应实现时不编名字
    assert rd.adapter_tool_for("resource.read", "knowledge") == ""
    assert rd.adapter_tool_for("resource.read", "knowledge", fallback="query_knowledge") == (
        "query_knowledge"
    )


def test_adapter_tool_for_matches_legacy_static_mapping():
    """迁移期不变量：Adapter 给出的底层工具必须与既有静态映射一致（否则是回归）。"""
    from app.agents.capabilities.broker.dispatch import CAPABILITY_TOOL_MAP

    pairs = {
        ("resource.read", "workspace"): "workspace.read",
        ("resource.edit", "workspace"): "workspace.edit",
        ("resource.move", "workspace"): "workspace.move",
        ("resource.delete", "workspace"): "workspace.delete",
        ("artifact.create", "artifact"): "artifact.create",
    }
    for (capability, resource_type), legacy in pairs.items():
        expected = CAPABILITY_TOOL_MAP.get(legacy, "")
        assert rd.adapter_tool_for(capability, resource_type) == expected, legacy


def test_provider_ids_for_narrowing():
    assert rd.provider_ids_for("resource.write", "workspace") == frozenset(
        {"lumi.local.workspace", "lumi.local.git"}
    )
    assert rd.provider_ids_for("code.execute", "workspace") == frozenset({"lumi.local.code"})
    # 知识库只有声明（provider_id 为空）→ 空集合 = 不收窄
    assert rd.provider_ids_for("resource.read", "knowledge") == frozenset()
    assert rd.provider_ids_for("resource.read", "") == frozenset()


def test_adapter_snapshot_lists_every_provider():
    snapshot = rd.adapter_snapshot()
    assert len(snapshot) == len(rd.RESOURCE_PROVIDERS)
    names = {item["name"] for item in snapshot}
    assert {"workspace_provider", "git_provider", "code_provider", "artifact_provider"} <= names
    knowledge = next(item for item in snapshot if item["name"] == "knowledge_provider")
    assert knowledge["registered"] is False


# ── 3. Broker 按资源类型收窄（且不倒过来卡死）─────────────────


def _lease(capability: str, provider_id: str, *, heartbeat: float = 1.0):
    from lumi_contracts.plugins import ProviderLease

    return ProviderLease(
        capability=capability,
        provider_id=provider_id,
        user_id="u1",
        last_heartbeat_at=heartbeat,
        # expires_at<=0 视为"未设租约"（过期）——测试里必须给**绝对**有效期
        expires_at=time.time() + 3600,
    )


def _context():
    from app.agents.capabilities.contracts.context import AgentExecutionContext

    return AgentExecutionContext.from_metadata(user_id="u1", user_role="user")


def test_select_lease_narrows_by_provider_ids():
    from app.agents.capabilities.broker.dispatch import select_lease

    leases = [
        _lease("workspace.write", "lumi.local.workspace", heartbeat=1.0),
        _lease("workspace.write", "lumi.local.git", heartbeat=9.0),
    ]
    # 不收窄：最近心跳的赢（既有语义）
    head, reason = select_lease(leases, capability="workspace.write", context=_context())
    assert head is not None and head.provider_id == "lumi.local.git" and reason == "ok"
    # 收窄到 workspace_provider：git 被排除
    head, reason = select_lease(
        leases,
        capability="workspace.write",
        context=_context(),
        provider_ids=frozenset({"lumi.local.workspace"}),
    )
    assert head is not None and head.provider_id == "lumi.local.workspace"


def test_select_lease_keeps_candidates_when_narrowing_yields_nothing():
    """**声明缺失不该表现成"工具不可用"**：收窄后没有候选 → 按收窄前继续。"""
    from app.agents.capabilities.broker.dispatch import select_lease

    leases = [_lease("workspace.write", "lumi.local.workspace")]
    head, reason = select_lease(
        leases,
        capability="workspace.write",
        context=_context(),
        provider_ids=frozenset({"lumi.local.unknown_provider"}),
    )
    assert head is not None, "收窄为空时必须回退到收窄前的候选"
    assert head.provider_id == "lumi.local.workspace"
    assert reason == "ok"


def test_select_lease_without_candidates_still_reports_binding_miss():
    from app.agents.capabilities.broker.dispatch import select_lease

    head, reason = select_lease(
        [_lease("workspace.read", "lumi.local.workspace")],
        capability="workspace.write",
        context=_context(),
    )
    assert head is None
    assert "绑定" in reason


# ── 4. 路由接线：结构化字段与兼容回落 ────────────────────────


@pytest.mark.asyncio
async def test_route_uses_structured_target_when_enabled(dispatch_on):
    """开关打开时：结构化的能力/资源类型进入结论与打点。"""
    from app.agents.capabilities.policy.routing import MODE_SHADOW, maybe_route_capability

    class _Leases:
        def snapshot(self):
            return [_lease("workspace.write", "lumi.local.workspace")]

    decision = await maybe_route_capability(
        tool_name="workspace_write",
        args={"path": "a.txt"},
        context=_context(),
        lease_service=_Leases(),
        mode=MODE_SHADOW,
        capability="workspace.write",
        unified_capability="resource.write",
        resource_type="workspace",
        provider_name="workspace_provider",
    )
    assert decision.capability == "workspace.write"
    assert decision.unified_capability == "resource.write"
    assert decision.resource_type == "workspace"
    assert decision.shadow is True and decision.handled is False
    observation = decision.to_snapshot()
    assert observation["unified_capability"] == "resource.write"
    assert observation["resource_type"] == "workspace"
    assert observation["provider_name"] == "workspace_provider"


@pytest.mark.asyncio
async def test_route_falls_back_to_name_resolution_when_not_structured(dispatch_off):
    """不给结构化目标时逐字走旧解析（``capability_for_mcp_tool``）。"""
    from app.agents.capabilities.broker.dispatch import capability_for_mcp_tool
    from app.agents.capabilities.policy.routing import MODE_SHADOW, maybe_route_capability

    class _Leases:
        def snapshot(self):
            return [_lease("workspace.write", "lumi.local.workspace")]

    decision = await maybe_route_capability(
        tool_name="workspace_write",
        args={},
        context=_context(),
        lease_service=_Leases(),
        mode=MODE_SHADOW,
    )
    assert decision.capability == capability_for_mcp_tool("workspace_write")
    assert decision.unified_capability == ""
    assert decision.resource_type == ""


@pytest.mark.asyncio
async def test_try_capability_route_off_is_zero_cost(dispatch_on, monkeypatch):
    """``AGENT_CAPABILITY_ROUTING_MODE=off`` 时连结构化解析都不做（零开销）。"""
    from app.agents.skills import capability_route

    called: list[str] = []

    def _spy(*args, **kwargs):
        called.append("resolve")
        raise AssertionError("off 模式不该解析结构化目标")

    monkeypatch.setattr(capability_route, "capability_for_mcp_tool", lambda _n: (_ for _ in ()).throw(AssertionError("off 模式不该查能力")))
    import app.agents.capabilities.broker.resource_dispatch as module

    monkeypatch.setattr(module, "resolve_dispatch", _spy)
    result = await capability_route.try_capability_route(
        tool_name="workspace_write", args={}, user_id="u1", mode="off"
    )
    assert result is None
    assert called == []


@pytest.mark.asyncio
async def test_capability_result_metadata_carries_resource_fields(dispatch_on):
    """结果 metadata 带统一能力/资源类型：前端据此显示"正在写入工作区"。"""
    from app.agents.capabilities.policy.routing import MODE_ACTIVE, RoutingDecision
    from app.agents.skills.capability_route import skill_result_from_capability

    decision = RoutingDecision(
        handled=True,
        mode=MODE_ACTIVE,
        capability="workspace.write",
        tool_name="workspace_write",
        unified_capability="resource.write",
        resource_type="workspace",
        provider_name="workspace_provider",
        provider_id="lumi.local.workspace",
    )
    result = skill_result_from_capability(decision, tool_name="workspace_write")
    metadata = result.metadata
    assert metadata["unified_capability"] == "resource.write"
    assert metadata["resource_type"] == "workspace"
    assert metadata["provider_name"] == "workspace_provider"


@pytest.mark.asyncio
async def test_structured_resolution_failure_falls_back_to_legacy(dispatch_on, monkeypatch):
    """结构化解析抛异常 → 回到兼容解析，绝不让一次工具调用打挂。"""
    from app.agents.skills import capability_route
    import app.agents.capabilities.broker.resource_dispatch as module

    monkeypatch.setattr(module, "resolve_dispatch", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = await capability_route.try_capability_route(
        tool_name="workspace_write",
        args={},
        user_id="u1",
        mode="shadow",
    )
    # shadow 模式不改执行路径：返回 None 即"走旧路径"
    assert result is None
