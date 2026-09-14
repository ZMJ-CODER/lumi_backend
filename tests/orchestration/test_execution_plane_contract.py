"""执行位置 / 运行方式契约（``execution_plane`` + ``runtime_kind``）端到端回归。

背景：``deployment`` / ``isolation`` 混用了"在哪一侧"和"什么隔离方式"，前端无法稳定回答
"实际由服务端 Worker 还是客户端 Worker 执行"。本文件按用户方案逐链路校验：

1. **Provider 注册**：声明支持的位置与运行方式被登记（注册项快照可读）；
2. **ProviderLease**：租约记录本次实际绑定的位置与运行方式（含客户端上报、心跳纠正、Redis 往返）；
3. **CapabilitySelection**：Broker 选中结论带实际值；
4. **CapabilityResult**：结果记录实际执行来源（含兼容派生 ``executor_type``）；
5. **审计日志 + Job 快照**：保存当时的真实值（刷新后不丢）；
6. **插件 Manifest**：只当声明，不当实际执行结果。
"""

from __future__ import annotations

import asyncio

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityResult,
    Deployment,
    ExecutionPlane,
    PluginManifest,
    PluginSnapshot,
    ProviderLease,
    RuntimeKind,
    capability_ok,
    executor_type_for,
    parse_execution_plane,
    parse_runtime_kind,
    plane_for_deployment,
    runtime_kind_for_isolation,
)

from app.agents.capabilities.audit.audit import audit_record
from app.agents.capabilities.broker.broker import CapabilityBroker
from app.agents.capabilities.registry.builtin import register_builtin_providers
from app.agents.capabilities.catalog.legacy import capability_catalog
from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.broker.dispatch import CapabilityDispatchAdapter
from app.agents.capabilities.registry.registry import CapabilityRegistry
from app.api.v1 import capabilities as cap_api
from app.services.capability_lease import lease_id_for

WORKSPACE = "ws-plane"
USER = "u1"
CONV = "conv-plane"


# ── 0. 词表与映射（唯一判定处）──────────────────────────────────


def test_plane_and_runtime_vocabulary():
    assert {str(item) for item in ExecutionPlane} == {"server", "client"}
    assert {str(item) for item in RuntimeKind} == {"in_process", "worker", "container", "sandbox"}
    # 未知值不得被当成"另一侧"：位置保守取 client，运行方式保守取 in_process。
    assert parse_execution_plane("nonsense") is ExecutionPlane.CLIENT
    assert parse_runtime_kind("nonsense") is RuntimeKind.IN_PROCESS
    assert parse_execution_plane("SERVER") is ExecutionPlane.SERVER


def test_deployment_and_isolation_map_to_the_two_axes():
    assert plane_for_deployment(Deployment.CLIENT) is ExecutionPlane.CLIENT
    assert plane_for_deployment(Deployment.SERVER) is ExecutionPlane.SERVER
    assert plane_for_deployment(Deployment.LOCAL_DEV) is ExecutionPlane.CLIENT
    # 旧词表里的 worker 语义含糊：按服务端受限 Worker 处理（调用方应显式声明 plane）。
    assert plane_for_deployment(Deployment.WORKER) is ExecutionPlane.SERVER
    assert runtime_kind_for_isolation("in_process") is RuntimeKind.IN_PROCESS
    assert runtime_kind_for_isolation("restricted_worker") is RuntimeKind.WORKER
    assert runtime_kind_for_isolation("sandboxed") is RuntimeKind.CONTAINER
    # 兼容旧字段：进程内 → 该侧名字；其余 → 隔离方式。
    assert executor_type_for("server", "in_process") == "server"
    assert executor_type_for("client", "in_process") == "client"
    assert executor_type_for("client", "worker") == "worker"
    assert executor_type_for("server", "container") == "container"


# ── 1. Provider 注册 ────────────────────────────────────────────


def test_registration_records_declared_plane_and_runtime():
    registry = CapabilityRegistry()
    register_builtin_providers(registry=registry)
    rows = {item.provider_id: item.to_snapshot() for item in registry.providers()}
    # 服务端内置（产物渲染）：server + in_process
    artifact = rows["lumi.server.artifact"]
    assert (artifact["execution_plane"], artifact["runtime_kind"]) == ("server", "in_process")
    assert artifact["executor_type"] == "server"
    # 客户端代码域：client + in_process（Electron 主进程内执行）
    code = rows["lumi.local.code"]
    assert (code["execution_plane"], code["runtime_kind"]) == ("client", "in_process")
    assert code["executor_type"] == "client"
    # 声明值来自 Provider 自述，而不是"按 deployment 猜"
    assert "workspace.read@1" in rows["lumi.local.workspace"]["capabilities"]


def test_registration_accepts_explicit_worker_on_either_side():
    """worker 同时表示位置与隔离方式的问题：显式声明必须能区分两侧 Worker。"""
    registry = CapabilityRegistry()
    # hybrid 才允许两侧注册（local_only 只允许客户端），这里只验证位置轴本身。
    descriptor = CapabilityDescriptor(name="demo.echo", data_locality="hybrid")

    class _Provider:
        provider_id = "demo.worker.provider"

        @property
        def deployment(self):
            return Deployment.CLIENT

        @property
        def descriptors(self):
            return (descriptor,)

        async def invoke(self, invocation, *, context):  # pragma: no cover - 不在本用例执行
            raise NotImplementedError

    client_worker = registry.register(
        _Provider(),
        deployment=Deployment.CLIENT,
        execution_plane="client",
        runtime_kind="worker",
        check_catalog=False,
    )
    assert client_worker.execution_plane is ExecutionPlane.CLIENT
    assert client_worker.runtime_kind is RuntimeKind.WORKER
    assert client_worker.executor_type() == "worker"

    server_worker = registry.register(
        _Provider(),
        deployment=Deployment.WORKER,
        execution_plane="server",
        runtime_kind="worker",
        check_catalog=False,
    )
    assert server_worker.execution_plane is ExecutionPlane.SERVER
    assert server_worker.deployment is Deployment.WORKER
    # 同样的 "worker" 隔离方式，两侧靠 execution_plane 区分
    assert server_worker.executor_type() == "worker"
    assert server_worker.to_snapshot()["execution_plane"] == "server"


# ── 2. ProviderLease ───────────────────────────────────────────


def test_lease_records_actual_binding_and_heartbeat_can_correct_it():
    cap_api.lease_service._leases.clear()  # noqa: SLF001 - 干净起点
    leases = asyncio.run(
        cap_api.lease_service.register(
            provider_id="lumi.local.workspace",
            capabilities=[{"capability": "workspace.read", "contract_version": 1}],
            user_id=USER,
            device_id="dev-1",
            conversation_id=CONV,
            workspace_id=WORKSPACE,
            deployment="client",
            execution_plane="client",
            runtime_kind="worker",
        )
    )
    assert len(leases) == 1
    lease = leases[0]
    assert lease.plane() is ExecutionPlane.CLIENT
    assert lease.runtime() is RuntimeKind.WORKER
    assert lease.executor_type() == "worker"
    snapshot = lease.to_snapshot()
    assert (snapshot["execution_plane"], snapshot["runtime_kind"]) == ("client", "worker")
    assert snapshot["executor_type"] == "worker"

    # 心跳可以纠正运行方式（插件从 Worker 迁回进程内）
    renewed = asyncio.run(
        cap_api.lease_service.heartbeat(
            provider_id="lumi.local.workspace",
            user_id=USER,
            capabilities=[{"capability": "workspace.read", "contract_version": 1}],
            execution_plane="client",
            runtime_kind="in_process",
        )
    )
    assert len(renewed) == 1
    assert renewed[0].runtime() is RuntimeKind.IN_PROCESS
    assert renewed[0].executor_type() == "client"
    cap_api.lease_service._leases.clear()  # noqa: SLF001


def test_lease_defaults_from_deployment_when_client_does_not_declare():
    """老客户端不上报这两个字段：租约仍必须给出稳定值（不写空、不报错）。"""
    lease = ProviderLease(provider_id="p", capability="workspace.read", deployment="client")
    assert (lease.plane(), lease.runtime()) == (ExecutionPlane.CLIENT, RuntimeKind.IN_PROCESS)
    legacy = ProviderLease(provider_id="p", capability="workspace.read", deployment="worker")
    assert legacy.plane() is ExecutionPlane.SERVER


def test_lease_redis_payload_round_trip_keeps_plane():
    """跨 worker/重启后仍要能回答"谁在执行"：Redis 编解码必须保留这两个字段。"""
    from app.services import capability_lease_redis as clr

    lease = ProviderLease(
        provider_id="p",
        capability="workspace.read",
        deployment="client",
        execution_plane="client",
        runtime_kind="worker",
        expires_at=9_999_999_999.0,
    )
    payload = {
        "schema_version": clr.SCHEMA_VERSION,
        "lease_id": lease_id_for("p", "workspace.read"),
        "provider_id": lease.provider_id,
        "capability": lease.capability,
        "contract_version": 1,
        "deployment": str(lease.deployment),
        "execution_plane": str(lease.plane()),
        "runtime_kind": str(lease.runtime()),
        "trust_level": "official",
        "scope": "{}",
        "expires_at": lease.expires_at,
        "issued_at": lease.issued_at,
        "last_heartbeat_at": lease.last_heartbeat_at,
    }
    decoded = clr.RedisLeaseRegistry._decode_lease(payload)  # noqa: SLF001
    assert decoded is not None
    assert decoded.plane() is ExecutionPlane.CLIENT
    assert decoded.runtime() is RuntimeKind.WORKER
    # 老记录（没有这两个字段）也不得报错：按 deployment 推导
    legacy = clr.RedisLeaseRegistry._decode_lease(  # noqa: SLF001
        {**payload, "execution_plane": "", "runtime_kind": ""}
    )
    assert legacy is not None
    assert legacy.plane() is ExecutionPlane.CLIENT
    assert legacy.runtime() is RuntimeKind.IN_PROCESS


# ── 3. CapabilitySelection（Broker 选中结论）─────────────────────


def test_broker_selection_records_selected_provider_plane():
    lease = ProviderLease(
        provider_id="lumi.local.workspace",
        capability="workspace.read",
        contract_version=1,
        lease_id=lease_id_for("lumi.local.workspace", "workspace.read"),
        user_id=USER,
        workspace_id=WORKSPACE,
        conversation_id=CONV,
        deployment=Deployment.CLIENT,
        execution_plane="client",
        runtime_kind="worker",
        expires_at=9_999_999_999.0,
    )

    class _Leases:
        def snapshot(self, **_kwargs):
            return [lease]

        def leases_for(self, *_args, **_kwargs):
            return [lease]

        def sync_registry(self):
            return []

    broker = CapabilityBroker(leases=_Leases())
    selection = broker.select("workspace.read", binding=lease.binding())
    assert selection.provider_id == "lumi.local.workspace"
    snapshot = selection.to_snapshot()
    assert snapshot["execution_plane"] == "client"
    assert snapshot["runtime_kind"] == "worker"
    assert snapshot["executor_type"] == "worker", "worker 必须能区分是哪一侧的 Worker"


# ── 4. CapabilityResult（实际执行来源）───────────────────────────


class _RecordingCaller:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, provider_id, tool_name, args=None, **kwargs):
        self.calls.append({"provider_id": provider_id, "tool": tool_name, "args": args, **kwargs})
        return {"status": "ok", "content": "ok", "data": {}}


def test_dispatch_result_carries_actual_execution_source():
    lease = ProviderLease(
        provider_id="lumi.local.workspace",
        capability="workspace.read",
        contract_version=1,
        lease_id=lease_id_for("lumi.local.workspace", "workspace.read"),
        user_id=USER,
        device_id="dev-1",
        workspace_id=WORKSPACE,
        conversation_id=CONV,
        deployment=Deployment.CLIENT,
        execution_plane="client",
        runtime_kind="worker",
        health_status="healthy",
        expires_at=9_999_999_999.0,
    )

    class _Leases:
        def snapshot(self, **_kwargs):
            return [lease]

        def leases_for(self, *_args, **_kwargs):
            return [lease]

        async def refresh_from_redis(self):
            return [lease]

    caller = _RecordingCaller()
    adapter = CapabilityDispatchAdapter(lease_service=_Leases(), call_tool=caller)
    context = AgentExecutionContext.from_metadata(
        user_id=USER, conversation_id=CONV, workspace_id=WORKSPACE, device_id="dev-1"
    )
    outcome = asyncio.run(
        adapter.dispatch(
            capability="workspace.read",
            args={"action": "read", "path": "a.py"},
            context=context,
            tool_name="workspace_navigator",
        )
    )
    assert outcome.handled is True and outcome.result is not None
    assert outcome.execution_plane == "client" and outcome.runtime_kind == "worker"
    assert outcome.result.plane() is ExecutionPlane.CLIENT
    assert outcome.result.runtime() is RuntimeKind.WORKER
    assert outcome.result.executor_type() == "worker"
    payload = outcome.result.to_execution_payload()
    assert (payload["execution_plane"], payload["runtime_kind"]) == ("client", "worker")
    assert payload["executor_type"] == "worker"


def test_capability_result_helper_accepts_plane_and_runtime():
    result = capability_ok(
        {"ok": True}, capability="code.execute@1", execution_plane="server", runtime_kind="sandbox"
    )
    assert result.plane() is ExecutionPlane.SERVER
    assert result.runtime() is RuntimeKind.SANDBOX
    assert result.executor_type() == "sandbox"
    # 没填时按数据来源推导：本地执行 = 客户端
    local = CapabilityResult(capability="workspace.read@1", served_locally=True)
    assert local.plane() is ExecutionPlane.CLIENT
    server = CapabilityResult(capability="artifact.create@1")
    assert server.plane() is ExecutionPlane.SERVER
    assert server.executor_type() == "server"


# ── 5. 审计与 Job 快照（保存当时的真实值）────────────────────────


def test_audit_record_persists_actual_execution_source():
    from lumi_contracts.plugins import CapabilityInvocation

    result = capability_ok(
        {},
        capability="workspace.read@1",
        provider_id="lumi.local.workspace",
        execution_plane="client",
        runtime_kind="worker",
        served_locally=True,
    )
    record = audit_record(
        CapabilityInvocation(capability="workspace.read", arguments={"path": "a.py"}),
        result,
        device_id="dev-1",
        workspace_id=WORKSPACE,
    ).to_dict()
    assert (record["execution_plane"], record["runtime_kind"]) == ("client", "worker")
    assert record["executor_type"] == "worker"
    assert "arguments" not in record and "payload" not in record, "审计不含参数与正文"


def test_job_snapshot_keeps_plane_and_runtime():
    lease = ProviderLease(
        provider_id="lumi.local.workspace",
        capability="workspace.read",
        contract_version=1,
        device_id="dev-1",
        workspace_id=WORKSPACE,
        deployment="client",
        execution_plane="client",
        runtime_kind="worker",
        expires_at=9_999_999_999.0,
    )
    descriptor = CapabilityDescriptor(name="workspace.read", data_locality="local_only")
    snapshot = PluginSnapshot.from_leases(leases=[lease], capabilities=[descriptor])
    binding = snapshot.capability_snapshot()[0]
    assert (binding["execution_plane"], binding["runtime_kind"]) == ("client", "worker")
    assert binding["executor_type"] == "worker"
    provider = snapshot.to_snapshot()["providers"][0]
    assert provider["execution_plane"] == "client"
    assert provider["runtime_kind"] == "worker"


def test_catalog_snapshot_declares_plane_per_capability():
    rows = {row["capability"]: row for row in capability_catalog.to_snapshot()}
    assert rows["workspace.read@1"]["execution_plane"] == "client"
    assert rows["artifact.create@1"]["execution_plane"] == "server"
    assert rows["workspace.read@1"]["runtime_kind"] == "in_process"
    assert rows["workspace.read@1"]["executor_type"] == "client"
    # 工作区操作能力由服务端操作网关编排
    assert rows["workspace.edit@1"]["execution_plane"] == "client"


def test_capability_event_frame_carries_execution_source():
    """SSE 出口：能力事件必须带执行来源，前端不必按 deployment 猜。"""
    from app.contracts.events import SseEventEncoder
    from app.services.capability_events import events_for_result

    result = capability_ok(
        {"ok": True},
        capability="workspace.read@1",
        provider_id="lumi.local.workspace",
        execution_plane="client",
        runtime_kind="worker",
        served_locally=True,
    )
    event = events_for_result(result, capability="workspace.read@1", job_id="job-1")
    frame = SseEventEncoder(job_id="job-1").frame(event)
    assert frame["type"] == "capability_completed"
    assert frame["execution_plane"] == "client"
    assert frame["runtime_kind"] == "worker"
    assert frame["executor_type"] == "worker"
    # 服务端执行的能力同样明确
    server = events_for_result(
        capability_ok({}, capability="artifact.create@1", execution_plane="server", runtime_kind="in_process"),
        capability="artifact.create@1",
        job_id="job-1",
    )
    assert SseEventEncoder(job_id="job-1").frame(server)["executor_type"] == "server"


# ── 6. 插件 Manifest：只当声明 ──────────────────────────────────


def test_manifest_snapshot_marks_declared_values_as_declarations():
    manifest = PluginManifest(
        id="demo.plugin",
        version="1.0.0",
        kind="capability_provider",
        deployment="client",
        isolation="restricted_worker",
        entrypoints={"capability": "demo.echo"},
    )
    snapshot = manifest.to_snapshot()
    assert manifest.declared_plane() is ExecutionPlane.CLIENT
    assert manifest.declared_runtime() is RuntimeKind.WORKER
    assert snapshot["execution_plane"] == "client"
    assert snapshot["runtime_kind"] == "worker"
    # 声明值单独留一份：前端/审计可以明确区分"插件声称"与"实际执行"
    assert snapshot["declared_execution_plane"] == "client"
    assert snapshot["declared_runtime_kind"] == "worker"
    assert snapshot["executor_type"] == "worker"


def test_manifest_can_declare_server_worker_explicitly():
    manifest = PluginManifest(
        id="demo.server.plugin",
        version="1.0.0",
        kind="skill_plugin",
        deployment="server",
        isolation="restricted_worker",
        trust_level="official",  # restricted_worker 至少需要 official 信任级别
        execution_plane="server",
        runtime_kind="worker",
    )
    assert manifest.declared_plane() is ExecutionPlane.SERVER
    assert manifest.declared_runtime() is RuntimeKind.WORKER
    assert manifest.to_snapshot()["executor_type"] == "worker"


# ── 7. 客户端上报链路（register payload）────────────────────────


def test_register_payload_carries_per_provider_plane_and_runtime():
    req = cap_api.RegisterProviderRequest.model_validate(
        {
            "device_id": "dev-1",
            "workspace_id": WORKSPACE,
            "conversation_id": CONV,
            "providers": [
                {
                    "provider_id": "lumi.local.workspace",
                    "provider_version": "1.0.0",
                    "health_status": "healthy",
                    "execution_plane": "client",
                    "runtime_kind": "worker",
                    "capabilities": [{"capability": "workspace.read@1", "contract_version": 1}],
                }
            ],
        }
    )
    rows = cap_api._flatten_declarations(req)  # noqa: SLF001 - 直接验证归一化结果
    assert rows[0]["execution_plane"] == "client"
    assert rows[0]["runtime_kind"] == "worker"


def test_register_endpoint_leases_carry_reported_plane():
    cap_api.lease_service._leases.clear()  # noqa: SLF001
    response = asyncio.run(
        cap_api.register_capability_provider(
            req=cap_api.RegisterProviderRequest.model_validate(
                {
                    "device_id": "dev-1",
                    "workspace_id": WORKSPACE,
                    "conversation_id": CONV,
                    "providers": [
                        {
                            "provider_id": "lumi.local.workspace",
                            "execution_plane": "client",
                            "runtime_kind": "worker",
                            "capabilities": [
                                {"capability": "workspace.read@1", "contract_version": 1}
                            ],
                        }
                    ],
                }
            ),
            payload={"sub": USER},
        )
    )
    lease_rows = response["data"]["leases"]
    assert lease_rows and lease_rows[0]["execution_plane"] == "client"
    assert lease_rows[0]["runtime_kind"] == "worker"
    assert lease_rows[0]["executor_type"] == "worker"
    # 目录侧也能读到对应声明
    listing = asyncio.run(cap_api.list_capabilities(payload={"sub": USER}))
    providers = {row["provider_id"]: row for row in listing["data"]["providers"]}
    assert providers["lumi.local.workspace"]["execution_plane"] == "client"
    cap_api.lease_service._leases.clear()  # noqa: SLF001
