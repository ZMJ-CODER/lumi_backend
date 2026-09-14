"""阶段 3 后端回归：能力派发适配层 + executor 门禁分阶段切流 + Redis 租约跨 worker。

三条契约：

1. **按租约派发**：``manager.call_tool`` 必须收到 ``route={provider_id, plugin_id,
   lease_id}``；只按"工具可达"派发会让健康隔离与撤销通道失效。
2. **不静默回退**：写/执行能力（workspace.write / code.execute / git.operations）在
   没有可用租约时**结构化失败**；只读能力也只在调用方显式允许时回退旧路径。
3. **分阶段切流**：``off`` 零开销、``shadow`` 只打点不改路径、``read_only`` 只切只读、
   ``active`` 全切。

Redis 部分：租约/能力索引/健康三类 key 分离，且**跨 worker 可见**（Redis 权威 +
读缓存）；Redis 不可用时降级为纯进程内且不影响单 worker 行为。
"""

from __future__ import annotations

import asyncio

import pytest

from lumi_contracts.plugins import (
    CapabilityErrorCode,
    DataLocality,
    Deployment,
    ProviderHealth,
    ProviderLease,
)

from app.agents.capabilities.catalog.legacy import (
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
)
from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.broker.dispatch import (
    CAPABILITY_TOOL_MAP,
    NEVER_FALLBACK_CAPABILITIES,
    CapabilityDispatchAdapter,
    adapt_to_mcp_tool,
    select_lease,
)
from app.agents.capabilities.policy.routing import (
    MODE_ACTIVE,
    MODE_OFF,
    MODE_READ_ONLY,
    MODE_SHADOW,
    maybe_route_capability,
    normalize_mode,
    should_route,
)
from app.services.capability_lease import CapabilityLeaseService
from app.services.capability_lease_redis import (
    GLOBAL_INDEX_KEY,
    RedisLeaseRegistry,
    capability_index_key,
    health_key,
    lease_id_for,
)


class _FakeLeaseService:
    """只提供适配层需要的同步快照 + 可选的 Redis 同步。"""

    def __init__(self, leases: list[ProviderLease], *, redis: RedisLeaseRegistry | None = None) -> None:
        self._leases = leases
        self._redis = redis
        self.refresh_calls = 0

    def snapshot(self, *, purge: bool = True) -> list[ProviderLease]:
        return list(self._leases)

    async def refresh_from_redis(self) -> list[ProviderLease]:
        self.refresh_calls += 1
        return list(self._leases)


def _lease(**overrides) -> ProviderLease:
    payload = {
        "provider_id": "lumi.local.workspace",
        "capability": "workspace.read",
        "contract_version": 1,
        "lease_id": lease_id_for("lumi.local.workspace", "workspace.read"),
        "user_id": "u1",
        "device_id": "device-1",
        "workspace_id": "ws-1",
        "conversation_id": "c1",
        "deployment": Deployment.CLIENT,
        "plugin_id": "lumi.local.workspace",
        "provider_version": "1.0.0",
        "health_status": ProviderHealth.HEALTHY.value,
        "expires_at": 9_999_999_999.0,
    }
    payload.update(overrides)
    return ProviderLease(**payload)


def _context(**overrides) -> AgentExecutionContext:
    payload = {
        "user_id": "u1",
        "conversation_id": "c1",
        "workspace_id": "ws-1",
        "device_id": "device-1",
    }
    payload.update(overrides)
    return AgentExecutionContext.from_metadata(**payload)


class _RecordingCaller:
    """记录 call_tool 收到的 (name, tool, args, route)。"""

    def __init__(self, *, payload: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.payload = payload if payload is not None else {"status": "ok", "content": "done"}

    async def __call__(self, name, tool_name, args=None, **kwargs):
        self.calls.append({"name": name, "tool": tool_name, "args": dict(args or {}), **kwargs})
        return self.payload


# ── 派发适配层 ───────────────────────────────────────────────────


def test_capability_tool_map_covers_advertised_capabilities():
    assert CAPABILITY_TOOL_MAP["workspace.read"] == "workspace_navigator"
    assert CAPABILITY_TOOL_MAP["workspace.write"] == "workspace_write"
    assert CAPABILITY_TOOL_MAP["workspace.edit"] == "workspace_edit"
    assert CAPABILITY_TOOL_MAP["workspace.move"] == "workspace_move"
    assert CAPABILITY_TOOL_MAP["workspace.delete"] == "workspace_delete"
    assert CAPABILITY_TOOL_MAP["code.execute"] == "sandbox_run"
    assert CAPABILITY_TOOL_MAP["git.operations"] == "workspace_diff"
    # 四个操作能力都属"失败面含半截副作用"的写类能力：不允许静默回退旧路径。
    assert NEVER_FALLBACK_CAPABILITIES == {
        "workspace.write",
        "workspace.edit",
        "workspace.move",
        "workspace.delete",
        "code.execute",
        "git.operations",
    }
    # 目前是恒等映射，但保留这一层以便参数整形
    assert adapt_to_mcp_tool("workspace_read", {"path": "a"}) == ("workspace_read", {"path": "a"})


def test_dispatch_calls_mcp_tool_with_lease_route():
    caller = _RecordingCaller()
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([_lease()]), call_tool=caller
    )
    outcome = asyncio.run(
        adapter.dispatch(
            capability=CAPABILITY_WORKSPACE_READ,
            args={"action": "read", "path": "a.py"},
            context=_context(),
            task_id="job-1",
        )
    )
    assert outcome.handled is True
    assert outcome.result is not None and outcome.result.ok is True
    assert outcome.result.served_locally is True
    assert len(caller.calls) == 1
    call = caller.calls[0]
    # 关键：MCP 调用必须带租约路由，而不是只按工具名；执行来源（位置/运行方式）随路由下发。
    assert call["route"] == {
        "provider_id": "lumi.local.workspace",
        "plugin_id": "lumi.local.workspace",
        "lease_id": lease_id_for("lumi.local.workspace", "workspace.read"),
        "capability": "workspace.read@1",
        "execution_plane": "client",
        "runtime_kind": "in_process",
        "executor_type": "client",
    }
    # 派发结论也记录实际执行来源（审计/快照直接读它，不必再猜 deployment）。
    assert outcome.execution_plane == "client"
    assert outcome.runtime_kind == "in_process"
    assert outcome.to_snapshot()["executor_type"] == "client"
    assert outcome.result.execution_plane is not None
    assert str(outcome.result.plane()) == "client"
    assert call["tool"] == "workspace_navigator"
    assert call["workspace_id"] == "ws-1"
    # 派发前会同步一次权威租约（跨 worker 可见性）
    assert adapter._leases.refresh_calls == 1  # noqa: SLF001


def test_dispatch_skips_cloud_capabilities():
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([]), call_tool=_RecordingCaller()
    )
    outcome = asyncio.run(
        adapter.dispatch(capability="artifact.create", args={}, context=_context())
    )
    assert outcome.handled is False
    assert "cloud" in outcome.reason


def test_lease_selection_prefers_healthy_and_most_recent_heartbeat():
    stale = _lease(provider_id="old", last_heartbeat_at=1.0)
    fresh = _lease(provider_id="new", last_heartbeat_at=100.0)
    unhealthy = _lease(
        provider_id="sick", last_heartbeat_at=200.0, health_status=ProviderHealth.UNHEALTHY.value
    )
    chosen, reason = select_lease(
        [stale, fresh, unhealthy], capability=CAPABILITY_WORKSPACE_READ, context=_context()
    )
    assert reason == "ok"
    assert chosen is not None and chosen.provider_id == "new"

    # 只有不健康的候选 → 明确报告健康问题（不是"没能力"）
    chosen, reason = select_lease(
        [unhealthy], capability=CAPABILITY_WORKSPACE_READ, context=_context()
    )
    assert chosen is None and "健康" in reason


def test_expired_lease_is_not_used_and_reports_lease_expired():
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([_lease(expires_at=1.0)]), call_tool=_RecordingCaller()
    )
    outcome = asyncio.run(
        adapter.dispatch(capability=CAPABILITY_WORKSPACE_READ, args={}, context=_context())
    )
    assert outcome.handled is True
    assert outcome.result.error_code == CapabilityErrorCode.LEASE_EXPIRED.value
    assert outcome.result.retryable is True


def test_write_capability_without_lease_never_falls_back():
    """写/执行能力没有租约时**结构化失败**，即使调用方允许回退。

    注意顺序：审批门禁在选 Provider **之前**，所以没有审批上下文时先返回
    ``APPROVAL_REQUIRED``——这同样是"不执行"，且比"缺 Provider"更准确
    （用户需要的是去审批，而不是去装 Provider）。
    """
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([]), call_tool=_RecordingCaller()
    )
    outcome = asyncio.run(
        adapter.dispatch(
            capability=CAPABILITY_WORKSPACE_WRITE,
            args={"operation": "commit"},
            context=_context(),
            allow_legacy_fallback=True,
        )
    )
    assert outcome.handled is True, "写类能力不允许回退旧路径"
    assert outcome.result.error_code == CapabilityErrorCode.APPROVAL_REQUIRED.value
    assert outcome.result.retryable is True  # 补审批后同一次调用可重发


def test_write_capability_with_approval_then_reports_missing_lease():
    """给了有效审批后，才轮到租约判定：没有租约 → CAPABILITY_MISSING（不可重试）。"""
    from app.agents.capabilities.policy.policy_guard import (
        capability_fingerprint,
        issue_approval_token,
    )

    args = {"operation": "commit"}
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([]), call_tool=_RecordingCaller()
    )
    # 用与门禁同一函数签发（指纹基准 = 能力基名 + 参数），保证"批准的就是这次调用"。
    token = issue_approval_token("workspace.write", args)
    outcome = asyncio.run(
        adapter.dispatch(
            capability=CAPABILITY_WORKSPACE_WRITE,
            args=args,
            context=_context(),
            allow_legacy_fallback=True,
            approval_context={
                "fingerprint": token["fingerprint"],
                "capability": "workspace.write",
                "expires_at": token["expires_at"],
            },
        )
    )
    assert outcome.handled is True
    assert outcome.result.error_code == CapabilityErrorCode.CAPABILITY_MISSING.value
    assert outcome.result.retryable is False
    # 版本号不参与审批指纹（升版不该让已批准的调用失效）
    assert capability_fingerprint("workspace.write", args) == capability_fingerprint(
        "workspace.write@1", args
    )


def test_invalid_approval_fingerprint_is_rejected_before_calling_client():
    """参数变了 → 审批指纹不符 → APPROVAL_INVALID，且**不去调客户端**。"""
    caller = _RecordingCaller()
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([_lease(capability="workspace.write")]),
        call_tool=caller,
    )
    outcome = asyncio.run(
        adapter.dispatch(
            capability=CAPABILITY_WORKSPACE_WRITE,
            args={"operation": "commit", "message": "改为别的提交"},
            context=_context(),
            approval_context={
                "fingerprint": "0" * 64,  # 批准的是另一次调用
                "capability": "workspace.write",
            },
        )
    )
    assert outcome.handled is True
    assert outcome.result.error_code == CapabilityErrorCode.APPROVAL_INVALID.value
    assert caller.calls == [], "审批不符时绝不能调用客户端"


def test_read_capability_needs_no_approval():
    """只读能力不需要审批：直接按租约派发。"""
    caller = _RecordingCaller()
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([_lease()]), call_tool=caller
    )
    outcome = asyncio.run(
        adapter.dispatch(
            capability=CAPABILITY_WORKSPACE_READ, args={"action": "read"}, context=_context()
        )
    )
    assert outcome.handled is True and outcome.result.ok is True
    assert len(caller.calls) == 1


def test_read_capability_without_lease_can_fall_back_when_allowed():
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([]), call_tool=_RecordingCaller()
    )
    allowed = asyncio.run(
        adapter.dispatch(
            capability=CAPABILITY_WORKSPACE_READ,
            args={},
            context=_context(),
            allow_legacy_fallback=True,
        )
    )
    assert allowed.handled is False, "只读能力显式允许时才回退旧路径"
    denied = asyncio.run(
        adapter.dispatch(
            capability=CAPABILITY_WORKSPACE_READ,
            args={},
            context=_context(),
            allow_legacy_fallback=False,
        )
    )
    assert denied.handled is True and denied.result.ok is False


def test_client_error_code_is_preserved_not_collapsed():
    caller = _RecordingCaller(
        payload={"status": "error", "error": "本机策略拒绝", "error_code": "POLICY_DENIED"}
    )
    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([_lease()]), call_tool=caller
    )
    outcome = asyncio.run(
        adapter.dispatch(capability=CAPABILITY_WORKSPACE_READ, args={}, context=_context())
    )
    assert outcome.result.error_code == CapabilityErrorCode.POLICY_DENIED.value
    assert outcome.result.retryable is False


def test_mcp_call_failure_becomes_provider_offline():
    class _NoneCaller:
        async def __call__(self, *args, **kwargs):
            return None

    adapter = CapabilityDispatchAdapter(
        lease_service=_FakeLeaseService([_lease()]), call_tool=_NoneCaller()
    )
    outcome = asyncio.run(
        adapter.dispatch(capability=CAPABILITY_WORKSPACE_READ, args={}, context=_context())
    )
    assert outcome.result.error_code == CapabilityErrorCode.PROVIDER_OFFLINE.value
    assert outcome.result.retryable is True


# ── 分阶段切流 ───────────────────────────────────────────────────


def test_mode_normalization_accepts_aliases():
    assert normalize_mode("shadow") == MODE_SHADOW
    assert normalize_mode("workspace_read_only") == MODE_READ_ONLY
    assert normalize_mode("READ_ONLY") == MODE_READ_ONLY
    assert normalize_mode("active") == MODE_ACTIVE
    assert normalize_mode("") == MODE_OFF
    assert normalize_mode("nonsense") == MODE_OFF


def test_should_route_only_reads_in_read_only_mode():
    assert should_route(MODE_OFF, CAPABILITY_WORKSPACE_READ) is False
    assert should_route(MODE_SHADOW, CAPABILITY_WORKSPACE_READ) is False
    assert should_route(MODE_READ_ONLY, CAPABILITY_WORKSPACE_READ) is True
    assert should_route(MODE_READ_ONLY, CAPABILITY_WORKSPACE_WRITE) is False
    assert should_route(MODE_READ_ONLY, CAPABILITY_CODE_EXECUTE) is False
    assert should_route(MODE_ACTIVE, CAPABILITY_CODE_EXECUTE) is True


def test_off_mode_does_not_touch_leases_at_all():
    leases = _FakeLeaseService([_lease()])
    decision = asyncio.run(
        maybe_route_capability(
            tool_name="workspace_navigator", args={}, context=_context(),
            lease_service=leases, mode=MODE_OFF,
        )
    )
    assert decision.handled is False
    assert leases.refresh_calls == 0, "off 模式必须零开销（不查租约）"


def test_shadow_mode_observes_but_does_not_change_execution_path():
    leases = _FakeLeaseService([_lease()])
    decision = asyncio.run(
        maybe_route_capability(
            tool_name="workspace_navigator", args={"action": "read"}, context=_context(),
            lease_service=leases, mode=MODE_SHADOW,
        )
    )
    assert decision.handled is False, "shadow 不得改执行路径"
    assert decision.shadow is True
    # 但打点信息完整：本应路由到哪个 Provider/租约
    assert decision.provider_id == "lumi.local.workspace"
    assert decision.lease_id == lease_id_for("lumi.local.workspace", "workspace.read")
    assert decision.observation["capability"] == "workspace.read"


def test_read_only_mode_routes_reads_but_leaves_writes_alone():
    caller = _RecordingCaller()
    adapter = CapabilityDispatchAdapter(lease_service=_FakeLeaseService([_lease()]), call_tool=caller)

    read_decision = asyncio.run(
        maybe_route_capability(
            tool_name="workspace_navigator", args={"action": "read"}, context=_context(),
            lease_service=adapter._leases, adapter=adapter, mode=MODE_READ_ONLY,  # noqa: SLF001
        )
    )
    assert read_decision.handled is True and read_decision.result.ok is True

    write_decision = asyncio.run(
        maybe_route_capability(
            tool_name="workspace_write", args={"operation": "commit"}, context=_context(),
            lease_service=adapter._leases, adapter=adapter, mode=MODE_READ_ONLY,  # noqa: SLF001
        )
    )
    assert write_decision.handled is False, "只读模式不得接管写能力"
    assert len(caller.calls) == 1, "写能力没有被派发"


def test_active_mode_blocks_write_without_lease_or_approval():
    """active 下写能力既没审批也没租约：先被审批拦下（APPROVAL_REQUIRED），不执行。"""
    decision = asyncio.run(
        maybe_route_capability(
            tool_name="workspace_write", args={"operation": "commit"}, context=_context(),
            lease_service=_FakeLeaseService([]), mode=MODE_ACTIVE,
        )
    )
    assert decision.handled is True
    assert decision.result.ok is False
    assert decision.result.error_code == CapabilityErrorCode.APPROVAL_REQUIRED.value


def test_local_action_tools_are_never_routed():
    for tool in ("desktop_open_app", "desktop_open_url", "user_clarify", "unknown_tool"):
        decision = asyncio.run(
            maybe_route_capability(
                tool_name=tool, args={}, context=_context(),
                lease_service=_FakeLeaseService([_lease()]), mode=MODE_ACTIVE,
            )
        )
        assert decision.handled is False and decision.shadow is False


# ── Redis 租约注册表 ─────────────────────────────────────────────


def test_lease_keys_are_split_into_three_namespaces():
    assert lease_id_for("p1", "workspace.read") == lease_id_for("p1", "workspace.read@1")
    assert lease_id_for("p1", "workspace.read") != lease_id_for("p2", "workspace.read")
    # 能力索引按工作区隔离；无工作区落全局 Sorted set
    assert capability_index_key("workspace.read", "ws-1") == "broker:capability:ws-1:workspace.read"
    assert capability_index_key("workspace.read", "") == GLOBAL_INDEX_KEY
    # 健康 key 独立于租约 key
    assert health_key("p1") == "broker:provider:p1:health"
    assert health_key("p1").startswith("broker:provider:")


def test_registry_degrades_gracefully_without_redis(monkeypatch):
    """Redis 不可用：写返回 0、读沿用缓存，绝不抛异常。"""
    registry = RedisLeaseRegistry()
    monkeypatch.setattr(RedisLeaseRegistry, "_redis", staticmethod(lambda: None))
    written = asyncio.run(registry.publish_leases([_lease()]))
    assert written == 0
    assert asyncio.run(registry.refresh()) == []
    assert registry.redis_available is False
    # 本地缓存仍可服务（单 worker 降级路径）
    registry.seed_cache([_lease()])
    assert len(registry.snapshot_cached()) == 1
    assert registry.health("lumi.local.workspace")["status"] == ProviderHealth.HEALTHY.value


def test_registry_cache_drops_expired_leases():
    registry = RedisLeaseRegistry()
    registry.seed_cache([_lease(expires_at=1.0), _lease(capability="workspace.write")])
    cached = registry.snapshot_cached()
    assert [lease.capability for lease in cached] == ["workspace.write"]


def test_lease_service_prefers_redis_snapshot_and_reports_cross_worker():
    """Redis 里有租约时，本地字典为空的 worker 也能看到（跨 worker 可见性）。"""
    from app.agents.capabilities.registry.registry import CapabilityRegistry

    registry = RedisLeaseRegistry()
    registry.seed_cache([_lease()])
    service = CapabilityLeaseService(
        registry=CapabilityRegistry(), redis_registry=registry
    )
    # 本地没有任何注册
    assert service._leases == {}  # noqa: SLF001
    snapshot = service.snapshot()
    assert [lease.capability for lease in snapshot] == ["workspace.read"]
    assert snapshot[0].lease_id == lease_id_for("lumi.local.workspace", "workspace.read")


@pytest.mark.parametrize(
    "capability",
    [CAPABILITY_WORKSPACE_READ, CAPABILITY_WORKSPACE_WRITE, CAPABILITY_CODE_EXECUTE],
)
def test_dispatch_tool_mapping_is_declared_for_local_capabilities(capability):
    """客户端会广告的本地能力都必须有 MCP 工具入口，否则派发无处可去。"""
    from app.agents.capabilities.catalog.legacy import capability_catalog

    descriptor = capability_catalog.require(capability)
    assert descriptor.data_locality is DataLocality.LOCAL_ONLY
    assert capability in CAPABILITY_TOOL_MAP
