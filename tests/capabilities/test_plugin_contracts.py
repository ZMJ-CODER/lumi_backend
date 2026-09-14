"""阶段 0 契约冻结回归：插件/能力契约的形状与**严格语义**。

这一层是后续所有阶段的地基，因此断言集中在"不许放宽"的地方：

1. ``local_only`` / ``cloud`` / ``hybrid`` 的路由判定（本地数据绝不静默上云）；
2. 未知插件类型 / 未知数据本地性**默认拒绝**，不是默认放行；
3. ``PluginManifest`` 的保守默认与一致性校验（隔离强度不得低于信任级别）；
4. ``CapabilityInvocation`` / ``CapabilityResult`` 的稳定错误码与结构化参数；
5. ``ProviderLease`` 的绑定匹配与过期即不可用；
6. 应用侧入口 ``lumi_contracts.plugins.*`` 与契约层同源（不复制字段定义）。
"""

from __future__ import annotations

import pytest

from lumi_contracts import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    DataLocality,
    Deployment,
    IsolationLevel,
    PluginKind,
    PluginManifest,
    PluginSnapshot,
    ProviderHealth,
    ProviderLease,
    SessionBinding,
    SideEffectKind,
    TrustLevel,
    ViewContribution,
    capability_failure,
    capability_ok,
    deployment_allows,
    parse_data_locality,
    parse_plugin_kind,
)


def _manifest(**overrides) -> PluginManifest:
    payload = {
        "id": "lumi.local.workspace",
        "version": "2.1.0",
        "kind": "capability_provider",
        "deployment": "client",
        "data_locality": "local_only",
        "isolation": "client_device",
        "trust_level": "official",
        "entrypoints": {"capability": "workspace.read"},
        "provides": {"capabilities": ["workspace.read@1"]},
    }
    payload.update(overrides)
    return PluginManifest(**payload)


# ── (1) 数据本地性语义 ───────────────────────────────────────────


def test_local_only_never_routes_to_server_even_with_policy_switch():
    """本地能力不允许被"策略允许切换"送上云端——这是数据泄露，不是降级。"""
    assert deployment_allows(DataLocality.LOCAL_ONLY, Deployment.CLIENT) is True
    assert deployment_allows(DataLocality.LOCAL_ONLY, Deployment.SERVER) is False
    assert (
        deployment_allows(
            DataLocality.LOCAL_ONLY, Deployment.SERVER, policy_allows_switch=True
        )
        is False
    )
    assert deployment_allows(DataLocality.LOCAL_ONLY, Deployment.WORKER) is False


def test_cloud_never_routes_to_client():
    assert deployment_allows(DataLocality.CLOUD, Deployment.SERVER) is True
    assert deployment_allows(DataLocality.CLOUD, Deployment.CLIENT) is False
    assert deployment_allows(DataLocality.CLOUD, Deployment.SERVER, policy_allows_switch=True) is True


def test_hybrid_switch_requires_explicit_policy():
    """hybrid 同侧可直接执行；换位置必须留下策略授权的痕迹。"""
    assert deployment_allows(DataLocality.HYBRID, Deployment.SERVER) is False
    assert deployment_allows(DataLocality.HYBRID, Deployment.SERVER, policy_allows_switch=True) is True
    # 客户端侧是 hybrid 的"本地"半边，不需要额外授权。
    assert deployment_allows(DataLocality.HYBRID, Deployment.CLIENT) is True
    # 未知部署位置一律拒绝（不发明新位置）。
    assert deployment_allows(DataLocality.HYBRID, "somewhere-else") is False


def test_unknown_locality_defaults_to_local_only():
    assert parse_data_locality("nonsense") is DataLocality.LOCAL_ONLY
    assert parse_data_locality("") is DataLocality.LOCAL_ONLY
    assert parse_data_locality("CLOUD") is DataLocality.CLOUD
    assert parse_data_locality(DataLocality.HYBRID) is DataLocality.HYBRID


# ── (2) 未知类型默认拒绝 ─────────────────────────────────────────


def test_unknown_plugin_kind_is_rejected_not_defaulted():
    assert parse_plugin_kind("skill_plugin") is PluginKind.SKILL_PLUGIN
    assert parse_plugin_kind("extension_handler") is PluginKind.EXTENSION_HANDLER
    assert parse_plugin_kind("view_plugin") is PluginKind.VIEW_PLUGIN
    # 未知类型解析必须返回 None，让调用方显式拒绝。
    assert parse_plugin_kind("quantum_plugin") is None
    assert parse_plugin_kind("") is None
    with pytest.raises(ValueError, match="未知插件类型"):
        _manifest(kind="quantum_plugin")


# ── (3) Manifest 的保守默认与一致性 ──────────────────────────────


def test_manifest_defaults_are_conservative():
    manifest = PluginManifest(id="lumi.code_expert", version="1.0.0", kind="skill_plugin")
    assert manifest.deployment is Deployment.SERVER
    # 未声明本地性 → 最保守（本地私有），不是"随便哪都行"。
    assert manifest.data_locality is DataLocality.LOCAL_ONLY
    # 未声明信任 → 第三方；隔离要求随之升到 sandboxed。
    assert manifest.trust_level is TrustLevel.THIRD_PARTY
    assert manifest.isolation is IsolationLevel.SANDBOXED
    assert manifest.signature.verified is False
    assert manifest.needs_approval is False


def test_manifest_rejects_weak_isolation_and_bad_ids():
    with pytest.raises(ValueError, match="隔离强度不足"):
        _manifest(trust_level="official", isolation="in_process", deployment="server")
    with pytest.raises(ValueError, match="非法插件 id"):
        _manifest(id="Bad Id")
    with pytest.raises(ValueError, match="非法插件版本"):
        _manifest(version="v1")
    with pytest.raises(ValueError, match="非法能力名"):
        _manifest(provides={"capabilities": ["workspaceread"]})


def test_client_provider_manifest_must_not_declare_server_module():
    """客户端插件的代码不在服务端加载：声明 module 入口必须被拒绝。"""
    with pytest.raises(ValueError, match="不得声明服务端 module"):
        _manifest(entrypoints={"capability": "workspace.read", "module": "evil.mod"})


def test_manifest_side_effects_drive_approval_and_snapshot():
    readonly = _manifest(
        entrypoints={"capability": "workspace.read"},
        side_effects=["read"],
    )
    assert readonly.needs_approval is False
    writer = _manifest(
        entrypoints={"capability": "workspace.write"},
        side_effects=[SideEffectKind.WRITE.value, SideEffectKind.EXECUTE.value],
    )
    assert writer.needs_approval is True
    snapshot = writer.to_snapshot()
    assert snapshot["id"] == "lumi.local.workspace"
    assert snapshot["kind"] == "capability_provider"
    assert snapshot["deployment"] == "client"
    assert len(snapshot["digest"]) == 64
    # digest 不随验签结果变化（验签是安装期事实，不属于自述摘要）。
    signed = writer.model_copy(deep=True)
    signed.signature.verified = True
    assert signed.digest() == writer.digest()


# ── (4) 调用与结果 ───────────────────────────────────────────────


def _descriptor(**overrides) -> CapabilityDescriptor:
    payload = {
        "name": "workspace.read",
        "contract_version": 1,
        "summary": "读取工作区文件",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "side_effects": ["read"],
        "data_locality": "local_only",
    }
    payload.update(overrides)
    return CapabilityDescriptor(**payload)


def test_invocation_normalizes_capability_version_and_idempotency():
    invocation = CapabilityInvocation(
        capability="workspace.read@1",
        arguments={"path": "src/app.py"},
        request_id="req-1",
    )
    assert invocation.capability == "workspace.read"
    assert invocation.contract_version == 1
    assert invocation.qualified_capability == "workspace.read@1"
    # 幂等键缺省复用 request_id：重试必须用同一个键才有意义。
    assert invocation.idempotency_key == "req-1"
    with pytest.raises(ValueError, match="非法能力名"):
        CapabilityInvocation(capability="notacapability")


def test_descriptor_requires_typed_schema_and_derives_sensitivity():
    descriptor = _descriptor()
    assert descriptor.qualified_name == "workspace.read@1"
    assert descriptor.sensitivity == "private"  # local_only → 默认私有
    cloud = _descriptor(name="web.search", data_locality="cloud", side_effects=["network"])
    assert cloud.sensitivity == "internal"
    assert cloud.allows_deployment(Deployment.SERVER) is True
    assert cloud.allows_deployment(Deployment.CLIENT) is False
    with pytest.raises(ValueError, match="必须声明 type"):
        _descriptor(input_schema={"properties": {}})


def test_result_error_codes_are_stable_and_classified():
    failure = capability_failure(
        CapabilityErrorCode.PROVIDER_OFFLINE,
        "客户端未连接",
        capability="workspace.read@1",
        provider_id="lumi.local.workspace",
    )
    assert failure.ok is False
    assert failure.error_code == "PROVIDER_OFFLINE"
    # 离线可以原样重试（幂等键不变）；这不是"工具调用失败"而是结构性状态。
    assert failure.retryable is True
    assert failure.needs_install is False
    unavailable = capability_failure(CapabilityErrorCode.CAPABILITY_UNAVAILABLE)
    assert unavailable.needs_install is True
    assert unavailable.retryable is False
    denied = capability_failure(CapabilityErrorCode.LOCAL_POLICY_DENIED, retryable=False)
    assert denied.retryable is False
    success = capability_ok(
        {"content": "ok"}, capability="workspace.read@1", served_locally=True
    )
    assert success.ok is True
    assert success.served_locally is True
    assert success.to_execution_payload()["capability"] == "workspace.read@1"


def test_view_contribution_is_declarative_only():
    view = ViewContribution(view_type="table", data={"rows": []}, source="workspace.read@1")
    assert view.view_type == "table"
    assert view.to_snapshot()["source"] == "workspace.read@1"
    # 未知视图类型拒绝（自定义交互必须走隔离容器，不是新 view_type）。
    with pytest.raises(ValueError, match="未知视图类型"):
        ViewContribution(view_type="arbitrary_jsx")


# ── (5) 租约 ─────────────────────────────────────────────────────


def _lease(**overrides) -> ProviderLease:
    payload = {
        "provider_id": "lumi.local.workspace",
        "capability": "workspace.read",
        "contract_version": 1,
        "user_id": "u1",
        "device_id": "device-1",
        "workspace_id": "ws-1",
        "conversation_id": "c1",
        "deployment": "client",
        "plugin_id": "lumi.local.workspace",
        "plugin_version": "2.1.0",
        "provider_version": "2.1.0",
        "expires_at": 1_000_000.0,
    }
    payload.update(overrides)
    return ProviderLease(**payload)


def test_lease_binding_requires_exact_workspace_and_device():
    lease = _lease()
    assert lease.qualified_capability == "workspace.read@1"
    assert lease.matches(SessionBinding(user_id="u1", device_id="device-1", workspace_id="ws-1"))
    # 换工作区/换设备/换用户都不能命中同一个租约。
    assert not lease.matches(SessionBinding(user_id="u1", device_id="device-1", workspace_id="ws-2"))
    assert not lease.matches(SessionBinding(user_id="u1", device_id="device-2", workspace_id="ws-1"))
    assert not lease.matches(SessionBinding(user_id="u2", device_id="device-1", workspace_id="ws-1"))
    # 租约绑了会话：换会话不能命中；没绑会话才算"该用户/设备上通用"。
    assert not lease.matches(
        SessionBinding(user_id="u1", device_id="device-1", conversation_id="c9")
    )
    assert lease.matches(
        SessionBinding(user_id="u1", device_id="device-1", conversation_id="c1")
    )
    unbound = _lease(conversation_id="")
    assert unbound.matches(
        SessionBinding(user_id="u1", device_id="device-1", conversation_id="c9")
    )


def test_lease_expiry_and_health_gate_usability():
    lease = _lease(expires_at=100.0, health_status="healthy")
    assert lease.is_expired(now=200.0) is True
    assert lease.is_usable(now=200.0) is False
    assert lease.is_usable(now=50.0) is True
    # 未设租约（<=0）视为过期：注册必须显式给出 TTL。
    assert _lease(expires_at=0.0).is_expired() is True
    unhealthy = _lease(expires_at=100.0, health_status=ProviderHealth.UNHEALTHY)
    assert unhealthy.is_usable(now=50.0) is False
    # UNKNOWN（刚注册还没体检）仍然可用。
    assert _lease(expires_at=100.0, health_status=ProviderHealth.UNKNOWN).is_usable(now=50.0) is True
    renewed = lease.renew(ttl_seconds=60, now=1000.0)
    assert renewed.expires_at == 1060.0
    assert renewed.last_heartbeat_at == 1000.0


# ── (6) 快照与应用侧入口 ─────────────────────────────────────────


def test_plugin_snapshot_records_capability_provider_version_binding():
    leases = [
        _lease(),
        _lease(capability="workspace.write", provider_version="2.1.0", plugin_id="lumi.local.workspace"),
        # 同 (能力, Provider, 设备) 重复注册只留一条
        _lease(capability="workspace.write", provider_version="2.1.0"),
    ]
    snapshot = PluginSnapshot.from_leases(
        leases=leases,
        capabilities=[_descriptor()],
    )
    binding = snapshot.capability_snapshot()
    assert len(binding) == 2
    assert binding[0]["capability"] == "workspace.read@1"
    assert binding[0]["data_locality"] == "local_only"
    assert binding[0]["deployment"] == "client"
    assert binding[0]["device_id"] == "device-1"
    assert [item.id for item in snapshot.providers] == ["lumi.local.workspace"]
    assert snapshot.providers[0].deployment == "client"
    assert snapshot.providers[0].plugin_id == "lumi.local.workspace"
    assert snapshot.to_snapshot()["policies"] == []


def test_plugin_contracts_have_exactly_one_home():
    """P7 之后插件契约**只有一个家**：``lumi_contracts.plugins``。

    过去 ``app/contracts/plugins/*`` 是 8 个转发壳（"应用侧入口"），它们的存在让
    "字段定义在哪"变成两个答案。现在壳已删除，这里断言：旧路径彻底消失，
    真实来源仍可直接使用。
    """
    import importlib

    import pytest

    for module in (
        "app.contracts.plugins",
        "app.contracts.plugins.manifest",
        "app.contracts.plugins.capability_result",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    from lumi_contracts.plugins.capability_result import CapabilityResult
    from lumi_contracts.plugins.manifest import PluginManifest
    from lumi_contracts.plugins.provider_lease import ProviderLease

    assert CapabilityResult.__module__.startswith("lumi_contracts")
    assert PluginManifest.__module__.startswith("lumi_contracts")
    assert ProviderLease.__module__.startswith("lumi_contracts")


# ── (7) 与前端既有词表对齐（前端已按阶段 0 手写类型，词表必须逐字一致）──


def test_vocabulary_is_superset_of_frontend_plugin_types():
    """前端 ``src/types/plugins.ts`` 已在用的取值必须在后端词表里存在。

    否则前端 `switch (error_code)` 拿到未知值只能落到 default——"安装/启用 Provider"
    入口、健康状态、部署位置都会静默失效。
    """
    from lumi_contracts import (
        ACTIVATABLE_PLUGIN_KINDS,
        DEVELOPER_ONLY_PLUGIN_KINDS,
        CapabilityStatus,
        ProviderHealth,
    )

    deployments = {str(item) for item in Deployment}
    assert {"server", "client", "local_dev"} <= deployments
    kinds = {str(item) for item in PluginKind}
    assert set(ACTIVATABLE_PLUGIN_KINDS) | set(DEVELOPER_ONLY_PLUGIN_KINDS) == kinds
    health = {str(item) for item in ProviderHealth}
    assert {"healthy", "degraded", "unhealthy", "offline", "unknown"} == health
    statuses = {str(item) for item in CapabilityStatus}
    assert {
        "idle", "requested", "waiting_provider", "waiting_approval",
        "running", "completed", "failed", "denied", "unavailable",
    } == statuses
    codes = {str(item) for item in CapabilityErrorCode}
    for required in (
        "INVALID_ARGUMENTS", "CAPABILITY_MISSING", "CAPABILITY_UNAVAILABLE",
        "PROVIDER_OFFLINE", "LEASE_EXPIRED", "POLICY_DENIED", "APPROVAL_REQUIRED",
        "APPROVAL_EXPIRED", "TIMEOUT", "CANCELLED", "UNKNOWN_PLUGIN_KIND",
        "CONTRACT_VERSION_MISMATCH", "INVALID_RESULT",
    ):
        assert required in codes, f"前端在用的能力错误码缺失：{required}"


def test_deployment_allows_accepts_local_dev_site():
    """``local_dev`` 是开发者模式的本地执行位置，按客户端侧规则判定。"""
    assert deployment_allows(DataLocality.LOCAL_ONLY, Deployment.LOCAL_DEV) is True
    assert deployment_allows(DataLocality.CLOUD, Deployment.LOCAL_DEV) is False


def test_capability_status_projection_maps_errors_to_display_states():
    """错误码 → 前端展示状态只由后端判定（前端不自己归类）。"""
    from lumi_contracts import CapabilityStatus, capability_status_for

    assert capability_status_for(ok=True) is CapabilityStatus.COMPLETED
    assert (
        capability_status_for(error_code="PROVIDER_OFFLINE") is CapabilityStatus.UNAVAILABLE
    )
    assert (
        capability_status_for(error_code="CAPABILITY_MISSING") is CapabilityStatus.UNAVAILABLE
    )
    assert (
        capability_status_for(error_code="APPROVAL_REQUIRED")
        is CapabilityStatus.WAITING_APPROVAL
    )
    assert capability_status_for(error_code="POLICY_DENIED") is CapabilityStatus.DENIED
    assert capability_status_for(error_code="LOCAL_POLICY_DENIED") is CapabilityStatus.DENIED
    assert capability_status_for(error_code="INVALID_ARGUMENTS") is CapabilityStatus.FAILED
    assert capability_status_for(error_code="") is CapabilityStatus.FAILED
