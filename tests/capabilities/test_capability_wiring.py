"""阶段 2 接线回归：Job 快照、能力状态 SSE 帧、REST 端点注册。

阶段 2 有两条容易"代码写了但没接上"的路径：能力状态事件没进 SSE 出口、快照没进 Job。
这里用最小依赖把它们钉住（不启动服务、不连 Redis）。
"""

from __future__ import annotations

import json

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    DataLocality,
    ProviderHealth,
    ProviderLease,
    SideEffectKind,
)

from app.agents.capabilities import CAPABILITY_WORKSPACE_READ, capability_catalog
from app.agents.capabilities.views.snapshots import (
    ROUTING_CAPABILITY_SNAPSHOT,
    ROUTING_PLUGIN_SNAPSHOT,
    ROUTING_POLICY_SNAPSHOT,
    attach_job_snapshots,
    build_job_snapshots,
    policy_refs,
)
from app.contracts.events import SseEventEncoder
from app.services.capability_events import CAPABILITY_EVENT_TYPES, build_capability_event


def _lease(**overrides) -> ProviderLease:
    payload = {
        "provider_id": "lumi.local.workspace",
        "capability": CAPABILITY_WORKSPACE_READ,
        "contract_version": 1,
        "user_id": "u1",
        "device_id": "device-1",
        "workspace_id": "ws-1",
        "deployment": "client",
        "plugin_id": "lumi.local.workspace",
        "plugin_version": "2.1.0",
        "provider_version": "2.1.0",
        "health_status": ProviderHealth.HEALTHY.value,
    }
    payload.update(overrides)
    return ProviderLease(**payload)


def test_job_snapshots_record_provider_device_and_contract_version():
    routing: dict = {}
    attach_job_snapshots(
        routing,
        approval_mode="manual_commit",
        execution_mode="step_confirm",
        policy_id="manual_commit",
        capability_broker=_BrokerStub([_lease()]),
    )
    plugin = routing[ROUTING_PLUGIN_SNAPSHOT]
    capabilities = routing[ROUTING_CAPABILITY_SNAPSHOT]
    policies = routing[ROUTING_POLICY_SNAPSHOT]
    assert plugin["providers"][0]["id"] == "lumi.local.workspace"
    assert plugin["providers"][0]["version"] == "2.1.0"
    assert plugin["providers"][0]["deployment"] == "client"
    assert capabilities[0]["capability"] == "workspace.read@1"
    assert capabilities[0]["device_id"] == "device-1"
    assert capabilities[0]["contract_version"] == 1
    assert capabilities[0]["data_locality"] == "local_only"
    # 策略快照包含策略包本身（含版本）+ 既有审批/执行模式，全部可审计。
    assert policies[0] == {"id": "manual_commit", "version": "1.0.0", "source": "builtin"}
    assert {row["id"] for row in policies} == {
        "manual_commit", "step_confirm",
    }
    # JSON-safe：直接能进 Redis/接口响应
    assert json.loads(json.dumps(routing, ensure_ascii=False)) == routing


def test_snapshot_is_downgraded_when_broker_unavailable():
    """快照是审计信息：取不到时不能阻断提交。"""
    routing: dict = {"execution_mode": "auto"}
    attach_job_snapshots(routing, capability_broker=_BrokenBroker())
    assert routing.get("execution_mode") == "auto"
    # 失败时不写半截快照（保持字段缺失而不是写入空壳）。
    assert ROUTING_CAPABILITY_SNAPSHOT not in routing


def test_policy_refs_and_empty_snapshot_shape():
    assert policy_refs() == []
    snapshot = build_job_snapshots()
    assert snapshot[ROUTING_PLUGIN_SNAPSHOT]["skills"] == []
    assert snapshot[ROUTING_CAPABILITY_SNAPSHOT] == []
    assert snapshot[ROUTING_POLICY_SNAPSHOT] == []


def test_capability_events_are_encoded_as_safe_process_frames():
    """能力帧走既有 SSE 出口，补齐统一过程字段，且不带参数/正文。"""
    encoder = SseEventEncoder(job_id="job-1")
    payload = build_capability_event(
        "capability_completed",
        job_id="job-1",
        capability="workspace.read@1",
        provider_id="lumi.local.workspace",
        provider_version="2.1.0",
        contract_version=1,
        device_id="device-1",
        status="completed",
    )
    line = encoder.encode(payload)
    frame = json.loads(line[len("data: "):].strip())
    assert frame["type"] == "capability_completed"
    assert frame["entry_id"]
    assert frame["kind"] in {"thinking", "read", "edit", "command", "tool", "system"}
    # 出口兜底文案：发射方没给 title/summary 也不能是空行。
    assert frame["title"] and frame["summary"]
    assert "workspace.read@1" in frame["summary"]
    assert frame["status"] in {"running", "completed", "failed", "pending"}
    assert frame["provider_version"] == "2.1.0"
    blob = json.dumps(frame, ensure_ascii=False)
    # 参数/正文绝不能出现在能力状态帧里。
    for forbidden in ("arguments", "payload", "reasoning"):
        assert forbidden not in blob


def test_capability_failure_frame_carries_structured_status():
    """失败帧必须带错误码与可重试标记（不能被压成"工具调用失败"）。"""
    encoder = SseEventEncoder(job_id="job-1")
    frame = encoder.frame(
        build_capability_event(
            "capability_failed",
            job_id="job-1",
            capability="workspace.read@1",
            provider_id="lumi.local.workspace",
            status="unavailable",
            error_code="PROVIDER_OFFLINE",
            retryable=True,
        )
    )
    assert frame["status"] == "unavailable"
    # 过程状态仍是契约合法值（能力状态另走 status，两者不互相覆盖）。
    assert frame["process_status"] in {"running", "completed", "failed", "pending"}
    assert frame["error_code"] == "PROVIDER_OFFLINE"
    assert frame["retryable"] is True
    assert frame["title"] and frame["summary"]


def test_capability_event_type_set_matches_frontend_contract():
    assert CAPABILITY_EVENT_TYPES == {
        "capability_requested",
        "waiting_provider",
        "provider_connected",
        "provider_disconnected",
        "capability_started",
        "capability_completed",
        "capability_failed",
        "plugin_health_changed",
        "approval_required",
    }


def test_capability_rest_router_is_registered():
    """能力端点必须挂进 /api/v1（prefix=/capabilities）。"""
    from app.api.router import api_router

    paths: set[str] = set()
    for route in api_router.routes:
        original = getattr(route, "original_router", None)
        if original is None:
            continue
        context = getattr(route, "include_context", None)
        prefix = str(getattr(context, "prefix", "") or "")
        if "capabilit" not in prefix:
            continue
        for sub in getattr(original, "routes", []):
            paths.add(str(getattr(sub, "path", "")))
    assert "" in paths  # GET /capabilities
    # 路由内的相对路径（挂载前缀是 /capabilities）
    assert "/register" in paths
    assert "/heartbeat" in paths
    assert "/unregister" in paths
    assert "/health" in paths
    # Broker 的唯一执行面 + 本地拒止回传 + 工具↔能力路由表
    assert "/invoke" in paths
    assert "/deny" in paths
    assert "/dispatch-map" in paths


def test_descriptor_snapshot_is_auditable():
    descriptor = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    snapshot = descriptor.to_snapshot()
    assert snapshot["capability"] == "workspace.read@1"
    assert snapshot["data_locality"] == DataLocality.LOCAL_ONLY.value
    assert snapshot["side_effects"] == [SideEffectKind.READ.value]
    assert snapshot["sensitivity"] == "private"


class _BrokerStub:
    """只暴露快照需要的接口（leases.snapshot / catalog.all）。"""

    def __init__(self, leases: list[ProviderLease]) -> None:
        self.leases = _LeasesStub(leases)
        self.catalog = capability_catalog


class _LeasesStub:
    def __init__(self, leases: list[ProviderLease]) -> None:
        self._leases = leases

    def snapshot(self, *, purge: bool = True) -> list[ProviderLease]:
        return list(self._leases)


class _BrokenBroker:
    @property
    def leases(self):
        raise RuntimeError("redis down")

    @property
    def catalog(self):
        raise RuntimeError("redis down")


def _descriptor() -> CapabilityDescriptor:
    return capability_catalog.require(CAPABILITY_WORKSPACE_READ)
