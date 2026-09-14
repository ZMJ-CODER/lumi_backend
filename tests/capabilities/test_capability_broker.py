"""阶段 2 回归：Capability Broker + 租约（注册/心跳/注销/过期 + 选路 + 幂等 + 超时）。

本阶段最容易出的问题是"看起来能跑，但语义被绕过"。因此断言集中在：

1. **绑定严格**：租约按 user/conversation/workspace/device 匹配，跨工作区/跨设备
   一律不命中（只按用户找 Provider 就是越权读文件）；
2. **过期即摘除**：租约过期后 Broker 选不到 Provider，返回结构化
   ``PROVIDER_OFFLINE``/``CAPABILITY_MISSING``，而不是"工具调用失败"；
3. **本地性不放宽**：``local_only`` 能力在服务端注册被拒；hybrid 默认留在本地，
   只有显式策略允许才切服务端；
4. **执行前失败**：缺能力/版本不符/参数非法都在调用前返回，不拖到中途；
5. **幂等**：同一幂等键重复调用不重复执行 Provider；
6. **超时**：Provider 超时返回 ``TIMEOUT`` 且可重试；
7. **事件**：调用前后发布能力状态事件（requested/started/completed|failed），
   且事件里没有参数正文。
"""

from __future__ import annotations

import asyncio
import itertools
import time

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    DataLocality,
    Deployment,
    ProviderHealth,
    SessionBinding,
    capability_failure,
    capability_ok,
)

from app.agents.capabilities import (
    CAPABILITY_ARTIFACT_CREATE,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    AgentExecutionContext,
    CapabilityCatalog,
    CapabilityRegistry,
    capability_catalog,
)
from app.agents.capabilities.broker.broker import CapabilityBroker, validate_arguments
from app.services.capability_lease import CapabilityLeaseService, LeaseRejected


class _RecordingProvider:
    """记录调用的假 Provider（可注入延迟/异常/结果）。"""

    def __init__(
        self,
        *,
        provider_id: str,
        descriptors: tuple[CapabilityDescriptor, ...],
        deployment: Deployment = Deployment.CLIENT,
        delay: float = 0.0,
        payload: dict | None = None,
        failure: str = "",
    ) -> None:
        self._provider_id = provider_id
        self._descriptors = descriptors
        self._deployment = deployment
        self.delay = delay
        self.payload = payload or {"status": "ok"}
        self.failure = failure
        self.calls: list[CapabilityInvocation] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def deployment(self) -> Deployment:
        return self._deployment

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return self._descriptors

    async def invoke(self, invocation, *, context) -> CapabilityResult:
        self.calls.append(invocation)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            return capability_failure(
                self.failure,
                "provider said no",
                capability=invocation.qualified_capability,
                provider_id=self._provider_id,
            )
        return capability_ok(
            dict(self.payload),
            capability=invocation.qualified_capability,
            provider_id=self._provider_id,
            served_locally=self._deployment is Deployment.CLIENT,
        )


def _stack(*, ttl: float = 60.0, delay: float = 0.0, failure: str = "", payload=None):
    """搭一套 (registry, leases, broker, provider)，能力为本地读取。"""
    registry = CapabilityRegistry()
    provider = _RecordingProvider(
        provider_id="lumi.local.workspace",
        descriptors=(capability_catalog.require(CAPABILITY_WORKSPACE_READ),),
        deployment=Deployment.CLIENT,
        delay=delay,
        failure=failure,
        payload=payload,
    )
    leases = CapabilityLeaseService(registry=registry, provider_resolver=lambda _lease: provider)

    async def setup():
        await leases.register(
            provider_id=provider.provider_id,
            capabilities=[{"capability": CAPABILITY_WORKSPACE_READ, "contract_version": 1}],
            user_id="u1",
            device_id="device-1",
            workspace_id="ws-1",
            conversation_id="c1",
            ttl_seconds=ttl,
            health_status=ProviderHealth.HEALTHY.value,
        )
        leases.sync_registry()

    asyncio.run(setup())
    broker = CapabilityBroker(registry=registry, leases=leases)
    return registry, leases, broker, provider


def _context(**overrides) -> AgentExecutionContext:
    payload = {
        "user_id": "u1",
        "conversation_id": "c1",
        "workspace_id": "ws-1",
        "device_id": "device-1",
    }
    payload.update(overrides)
    return AgentExecutionContext.from_metadata(**payload)


_REQ_COUNTER = itertools.count(1)


def _invocation(**overrides) -> CapabilityInvocation:
    """构造一次调用；``session_binding`` 默认交给服务端上下文（Broker 的既有语义）。

    幂等键**每次调用都不同**：只有显式复用同一个键的用例才应触发幂等重放，
    否则会把"上一次调用的结果"当成"这一次的行文"（测试里很容易踩）。
    """
    index = next(_REQ_COUNTER)
    payload = {
        "capability": CAPABILITY_WORKSPACE_READ,
        "arguments": {"action": "read", "path": "src/app.py"},
        "request_id": f"req-{index}",
        "idempotency_key": f"idem-{index}",
    }
    payload.update(overrides)
    return CapabilityInvocation(**payload)


def _invoke(broker, invocation=None, *, context=None, **kwargs):
    return asyncio.run(
        broker.invoke(
            invocation or _invocation(),
            context=context or _context(),
            **kwargs,
        )
    )


# ── (1) 租约注册与绑定 ───────────────────────────────────────────


def test_register_issues_leases_and_syncs_registry():
    registry, leases, _broker, _provider = _stack()
    lease = leases.leases_for(CAPABILITY_WORKSPACE_READ)[0]
    assert lease.provider_id == "lumi.local.workspace"
    assert lease.qualified_capability == "workspace.read@1"
    assert lease.device_id == "device-1"
    assert lease.expires_at > time.time()
    # 注册表里能查到实现（Broker 靠它转发）。
    assert [item.provider_id for item in registry.registrations(CAPABILITY_WORKSPACE_READ)] == [
        "lumi.local.workspace"
    ]


def test_register_rejects_undeclared_capability_and_wrong_side():
    leases = CapabilityLeaseService(registry=CapabilityRegistry())

    async def undeclared():
        await leases.register(
            provider_id="p1",
            capabilities=[{"capability": "nope.nothing"}],
            user_id="u1",
            ttl_seconds=30,
        )

    try:
        asyncio.run(undeclared())
    except LeaseRejected as exc:
        assert exc.code == CapabilityErrorCode.CAPABILITY_MISSING.value
    else:  # pragma: no cover
        raise AssertionError("未声明能力被接受")

    async def wrong_side():
        await leases.register(
            provider_id="p1",
            capabilities=[{"capability": CAPABILITY_WORKSPACE_READ}],
            user_id="u1",
            deployment=Deployment.SERVER.value,
            ttl_seconds=30,
        )

    try:
        asyncio.run(wrong_side())
    except LeaseRejected as exc:
        # 本地读取注册到服务端 = 数据可能离开本机，必须拒绝。
        assert exc.code == CapabilityErrorCode.DEPLOYMENT_NOT_ALLOWED.value
    else:  # pragma: no cover
        raise AssertionError("本地能力在服务端注册被接受")


def test_lease_expiry_detaches_capability_and_broker_reports_offline():
    registry, leases, broker, _provider = _stack(ttl=60.0)
    # 手工把租约推到过期（避免测试真的等 60s）。
    key = next(iter(leases._leases))  # noqa: SLF001 - 测试需要精确控制过期时间
    leases._leases[key] = leases._leases[key].model_copy(  # noqa: SLF001
        update={"expires_at": time.time() - 1}
    )
    leases.sync_registry()
    assert registry.registrations(CAPABILITY_WORKSPACE_READ) == []
    result = _invoke(broker)
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.CAPABILITY_MISSING.value
    assert result.needs_install is True


def test_binding_mismatch_does_not_reuse_lease():
    _registry, _leases, broker, provider = _stack()
    # 调用方自述绑定与服务端上下文不一致 → 直接 SCOPE_DENIED，不路由、不执行。
    invocation = _invocation(
        session_binding=SessionBinding(
            user_id="u1", conversation_id="c1", workspace_id="ws-2", device_id="device-1"
        )
    )
    result = _invoke(broker, invocation, context=_context(workspace_id="ws-1"))
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.SCOPE_DENIED.value
    assert provider.calls == []
    # 服务端上下文换工作区（调用方没说）→ 租约不匹配 → 无可用 Provider。
    result = _invoke(broker, context=_context(workspace_id="ws-2"))
    assert result.ok is False
    assert result.error_code in {
        CapabilityErrorCode.CAPABILITY_MISSING.value,
        CapabilityErrorCode.CAPABILITY_UNAVAILABLE.value,
    }
    assert provider.calls == []
    # 换设备同样不命中
    result = _invoke(broker, context=_context(device_id="device-9"))
    assert result.ok is False
    assert provider.calls == []


def test_heartbeat_renews_but_does_not_revive_expired_lease():
    _registry, leases, _broker, _provider = _stack(ttl=30.0)
    key = next(iter(leases._leases))  # noqa: SLF001
    base = leases._leases[key]  # noqa: SLF001

    async def renew():
        return await leases.heartbeat(provider_id="lumi.local.workspace", user_id="u1", ttl_seconds=120)

    renewed = asyncio.run(renew())
    assert renewed[0].expires_at > base.expires_at
    assert renewed[0].last_heartbeat_at > base.last_heartbeat_at

    leases._leases[key] = base.model_copy(update={"expires_at": time.time() - 1})  # noqa: SLF001

    async def revive():
        return await leases.heartbeat(
            provider_id="lumi.local.workspace", user_id="u1", ttl_seconds=30
        )

    # 过期租约不会被心跳复活：续期结果为**空**（必须重新注册、重新声明能力）。
    assert asyncio.run(revive()) == []
    assert leases.get(capability="workspace.read", provider_id="lumi.local.workspace") is not None


def test_unregister_removes_leases_and_registry_entries():
    registry, leases, broker, provider = _stack()

    async def drop():
        return await leases.unregister(provider_id="lumi.local.workspace", user_id="u1")

    removed = asyncio.run(drop())
    assert len(removed) == 1
    leases.sync_registry()
    assert registry.registrations(CAPABILITY_WORKSPACE_READ) == []
    result = _invoke(broker)
    assert result.ok is False
    assert provider.calls == []
    # 不能注销别人的 Provider
    _registry2, leases2, _broker2, _p2 = _stack()

    async def foreign():
        return await leases2.unregister(provider_id="lumi.local.workspace", user_id="u2")

    assert asyncio.run(foreign()) == []


# ── (2) Broker 选路 ─────────────────────────────────────────────


def test_cloud_capability_never_routes_to_client_provider():
    """cloud 能力（artifact.create）不允许注册到客户端。"""
    leases = CapabilityLeaseService(registry=CapabilityRegistry())

    async def register_client_side():
        await leases.register(
            provider_id="client.artifact",
            capabilities=[{"capability": CAPABILITY_ARTIFACT_CREATE}],
            user_id="u1",
            deployment=Deployment.CLIENT.value,
            ttl_seconds=30,
        )

    try:
        asyncio.run(register_client_side())
    except LeaseRejected as exc:
        assert exc.code == CapabilityErrorCode.DEPLOYMENT_NOT_ALLOWED.value
    else:  # pragma: no cover
        raise AssertionError("cloud 能力被注册到客户端")


def test_broker_selects_most_recent_heartbeat_among_candidates():
    registry = CapabilityRegistry()
    older = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    first = _RecordingProvider(
        provider_id="device-old", descriptors=(older,), deployment=Deployment.CLIENT
    )
    second = _RecordingProvider(
        provider_id="device-new", descriptors=(older,), deployment=Deployment.CLIENT
    )
    provider_by_id = {first.provider_id: first, second.provider_id: second}
    leases = CapabilityLeaseService(
        registry=registry, provider_resolver=lambda lease: provider_by_id[lease.provider_id]
    )

    async def setup():
        for pid in ("device-old", "device-new"):
            await leases.register(
                provider_id=pid,
                capabilities=[{"capability": CAPABILITY_WORKSPACE_READ}],
                user_id="u1",
                device_id="device-1",
                workspace_id="ws-1",
                ttl_seconds=60,
                health_status=ProviderHealth.HEALTHY.value,
            )
        # 拉到不同心跳时间：旧的更早，新的更近。
        for key, lease in list(leases._leases.items()):  # noqa: SLF001
            stamp = time.time() - (30 if "old" in key[1] else 0)
            leases._leases[key] = lease.model_copy(  # noqa: SLF001
                update={"last_heartbeat_at": stamp}
            )
        leases.sync_registry()

    asyncio.run(setup())
    broker = CapabilityBroker(registry=registry, leases=leases)
    selection = broker.select(
        CAPABILITY_WORKSPACE_READ,
        binding=SessionBinding(user_id="u1", device_id="device-1", workspace_id="ws-1"),
    )
    assert selection.provider_id == "device-new"


def test_hybrid_defaults_local_and_switch_needs_explicit_choice():
    """hybrid 能力默认留在本地；只有调用方显式要求才可能切到服务端。"""
    descriptor = capability_catalog.require(CAPABILITY_WORKSPACE_READ).model_copy(
        update={"data_locality": DataLocality.HYBRID}
    )
    registry = CapabilityRegistry()
    server = _RecordingProvider(
        provider_id="server.side", descriptors=(descriptor,), deployment=Deployment.SERVER
    )
    client = _RecordingProvider(
        provider_id="client.side", descriptors=(descriptor,), deployment=Deployment.CLIENT
    )
    provider_by_id = {server.provider_id: server, client.provider_id: client}
    # 用 hybrid 目录（真实部署里 hybrid 能力由插件声明，不在内置目录里）。
    hybrid_catalog = CapabilityCatalog((descriptor,))
    leases = CapabilityLeaseService(
        registry=registry,
        catalog=hybrid_catalog,
        provider_resolver=lambda lease: provider_by_id[lease.provider_id],
    )

    async def setup():
        for provider, site in ((server, Deployment.SERVER), (client, Deployment.CLIENT)):
            await leases.register(
                provider_id=provider.provider_id,
                capabilities=[{"capability": CAPABILITY_WORKSPACE_READ}],
                user_id="u1",
                device_id="device-1",
                workspace_id="ws-1",
                deployment=site.value,
                ttl_seconds=60,
                health_status=ProviderHealth.HEALTHY.value,
            )
        leases.sync_registry()

    asyncio.run(setup())
    broker = CapabilityBroker(registry=registry, leases=leases, catalog=hybrid_catalog)
    binding = SessionBinding(user_id="u1", device_id="device-1", workspace_id="ws-1")
    default_selection = broker.select(CAPABILITY_WORKSPACE_READ, binding=binding)
    assert default_selection.provider_id == "client.side"
    assert default_selection.routed_by_policy is False
    # 显式要求服务端 + 策略放行 → 才允许切换，并留下审计痕迹。
    switched = broker.select(
        CAPABILITY_WORKSPACE_READ,
        binding=binding,
        preferred_deployment=Deployment.SERVER.value,
        policy_allows_switch=True,
    )
    assert switched.provider_id == "server.side"
    assert switched.routed_by_policy is True


# ── (3) 执行前失败 / 参数校验 / 幂等 / 超时 ───────────────────────


def test_invalid_arguments_fail_before_provider_is_called():
    _registry, _leases, broker, provider = _stack()
    # action 必填且必须是枚举值之一。
    missing = _invoke(broker, _invocation(arguments={}))
    assert missing.error_code == CapabilityErrorCode.INVALID_ARGUMENTS.value
    assert provider.calls == []
    bad_enum = _invoke(broker, _invocation(arguments={"action": "explode"}))
    assert bad_enum.error_code == CapabilityErrorCode.INVALID_ARGUMENTS.value
    bad_type = _invoke(broker, _invocation(arguments={"action": "read", "max_chars": "many"}))
    assert bad_type.error_code == CapabilityErrorCode.INVALID_ARGUMENTS.value
    assert provider.calls == []


def test_contract_version_mismatch_is_reported_before_execution():
    _registry, _leases, broker, provider = _stack()
    result = _invoke(broker, _invocation(capability="workspace.read@2"))
    assert result.ok is False
    # 能力存在但版本不符：提示升级 Provider，而不是"缺能力"。
    assert result.error_code == CapabilityErrorCode.CONTRACT_VERSION_MISMATCH.value
    assert result.needs_install is True
    assert provider.calls == []


def test_idempotent_replay_does_not_call_provider_twice():
    _registry, _leases, broker, provider = _stack()
    invocation = _invocation()
    first = _invoke(broker, invocation)
    second = _invoke(broker, invocation)
    assert first.ok and second.ok
    assert len(provider.calls) == 1, "同一幂等键重复调用不应重复执行"
    # 换幂等键 → 真的再执行一次
    third = _invoke(broker, _invocation())
    assert third.ok
    assert len(provider.calls) == 2


def test_provider_timeout_returns_retryable_timeout():
    _registry, _leases, broker, provider = _stack(delay=0.5)
    result = _invoke(broker, _invocation(timeout_seconds=0.05, idempotency_key="t1"))
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.TIMEOUT.value
    assert result.retryable is True


def test_provider_failure_is_adopted_without_leaking_details():
    _registry, _leases, broker, _provider = _stack(failure="PROVIDER_OFFLINE")
    result = _invoke(broker, _invocation(idempotency_key="f1"))
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.PROVIDER_OFFLINE.value
    assert result.retryable is True


def test_validate_arguments_checks_required_types_and_enums():
    descriptor = capability_catalog.require(CAPABILITY_WORKSPACE_WRITE)
    problems = validate_arguments(descriptor, {})
    assert any("operation" in item for item in problems)
    assert validate_arguments(descriptor, {"operation": "commit"}) == []
    assert validate_arguments(descriptor, {"operation": "nope"})


# ── (4) 事件与快照 ──────────────────────────────────────────────


def test_capability_events_are_safe_and_structured(monkeypatch):
    """事件流：类型/状态/错误码在，参数与正文不在。"""
    published: list[dict] = []

    async def fake_publish(event_type, *, job_id="", **fields):
        from app.services.capability_events import build_capability_event

        published.append(build_capability_event(event_type, job_id=job_id, **fields))
        return True

    monkeypatch.setattr(
        "app.services.capability_events.publish_capability_event", fake_publish
    )
    monkeypatch.setattr(
        "app.agents.capabilities.broker.broker.publish_capability_event", fake_publish
    )
    _registry, _leases, broker, _provider = _stack()
    result = _invoke(broker, _invocation(arguments={"action": "read", "path": "secret/path.py"}))
    assert result.ok is True
    types = [item["type"] for item in published]
    assert "capability_requested" in types
    assert "capability_started" in types
    assert "capability_completed" in types
    blob = str(published)
    # 事件里绝不能出现参数内容（那是"过程状态"，不是数据通道）。
    assert "secret/path.py" not in blob
    completed = next(item for item in published if item["type"] == "capability_completed")
    assert completed["capability"] == "workspace.read@1"
    assert completed["status"] == "completed"


def test_broker_snapshot_records_provider_and_contract_version():
    _registry, _leases, broker, _provider = _stack()
    rows = broker.capability_snapshot()
    assert rows and rows[0]["capability"] == "workspace.read@1"
    assert rows[0]["provider_id"] == "lumi.local.workspace"
    assert rows[0]["deployment"] == "client"
    assert rows[0]["device_id"] == "device-1"
    assert rows[0]["contract_version"] == 1
    assert rows[0]["data_locality"] == "local_only"
    plugin_snapshot = broker.leases.plugin_snapshot()
    assert plugin_snapshot["capabilities"][0]["capability"] == "workspace.read@1"
    assert plugin_snapshot["providers"][0]["id"] == "lumi.local.workspace"
