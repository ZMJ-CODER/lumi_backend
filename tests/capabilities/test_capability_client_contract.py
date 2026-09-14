"""客户端联调契约回归：注册/心跳 payload 兼容、撤销、expires_at 语义、路由表、错误码。

客户端（Electron Provider Runtime）已经按这套契约实现，因此这里的断言是**两边合同**，
不是"我这边觉得应该这样"：

1. ``providers[].capabilities``（带真实归属）优先，旧 ``capabilities[]`` 兜底；
2. ``revoked_providers`` / ``revoked_capabilities`` **立即摘除**租约（不等 120s）；
3. 响应给 ``expires_at``（未来时间 = 继续续租；缺失/过去时间 = 客户端停止续租）；
4. ``dispatch-map`` 给出工具↔能力路由表，本机动作显式 ``null``；
5. 客户端在用的错误码后端全部存在（缺一个前端就会落到 default 分支）；
6. 客户端广告的 4 个能力与后端目录一致（本地性必须都是 ``local_only``）。
"""

from __future__ import annotations

import asyncio
import time

from lumi_contracts.plugins import (
    CapabilityErrorCode,
    DataLocality,
    ProviderHealth,
    RETRYABLE_CAPABILITY_ERRORS,
    capability_status_for,
)

from app.agents.capabilities.registry.builtin import capability_for_tool
from app.agents.capabilities.catalog.legacy import (
    CAPABILITY_ARTIFACT_CREATE,
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_CODE_SCAN,
    CAPABILITY_GIT_OPERATIONS,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    capability_catalog,
)
from app.api.v1 import capabilities as cap_api


def _reset_leases() -> None:
    """每个用例独立：清空进程内租约服务，避免相互污染。"""
    cap_api.lease_service._leases.clear()  # noqa: SLF001 - 测试需要干净起点


def _register(payload: dict):
    # 走请求模型（与真实 HTTP 入口一致：extra="allow" 的兼容行为必须被真正验证）。
    request = cap_api.RegisterProviderRequest.model_validate(payload)
    return asyncio.run(
        cap_api.register_capability_provider(req=request, payload={"sub": "u1"})
    )


def _heartbeat(payload: dict):
    request = cap_api.HeartbeatRequest.model_validate(payload)
    return asyncio.run(
        cap_api.heartbeat_capability_provider(req=request, payload={"sub": "u1"})
    )


def _client_payload(**overrides) -> dict:
    """客户端真实 payload 形状（含新旧两种能力写法与新字段）。"""
    payload = {
        "provider_id": "lumi.local.workspace",
        "provider_version": "1.0.0",
        "device_id": "device-1",
        "user_id": "u1",
        "conversation_id": "c1",
        "workspace_id": "ws-1",
        "session_id": "sess-1",
        "capabilities": [
            {
                "capability": "workspace.read@1",
                "provider_id": "lumi.local.workspace",
                "provider_version": "1.0.0",
                "contract_version": 1,
                "data_locality": "local_only",
                "requires_approval": False,
                "health_status": "healthy",
            }
        ],
        "providers": [
            {
                "provider_id": "lumi.local.workspace",
                "health_status": "healthy",
                "dispatchable": True,
                "plugin_id": "lumi.local.workspace",
                "provider_version": "1.0.0",
                "capabilities": [
                    {"capability": "workspace.read@1", "contract_version": 1},
                    {"capability": "workspace.write@1", "contract_version": 1},
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


# ── payload 兼容与归属 ───────────────────────────────────────────


def test_register_accepts_client_payload_and_honours_provider_grouping():
    _reset_leases()
    response = _register(_client_payload())
    data = response["data"]
    capabilities = sorted(row["capability"] for row in data["leases"])
    # providers[].capabilities 优先：read + write 都注册（旧写法里只有 read）
    assert capabilities == ["workspace.read@1", "workspace.write@1"]
    # 归属正确：两个能力都属于 lumi.local.workspace
    assert {row["provider_id"] for row in data["leases"]} == {"lumi.local.workspace"}
    # expires_at 是未来时间（客户端据此继续续租）
    assert data["expires_at"] > time.time()
    assert data["lease_ttl_seconds"] == 120.0


def test_register_falls_back_to_legacy_flat_capabilities():
    _reset_leases()
    response = _register(_client_payload(providers=[]))
    capabilities = [row["capability"] for row in response["data"]["leases"]]
    assert capabilities == ["workspace.read@1"]


def test_register_rejects_payload_without_any_capability():
    _reset_leases()
    try:
        _register(_client_payload(capabilities=[], providers=[]))
    except Exception as exc:  # noqa: BLE001 - BadRequestException
        assert getattr(exc, "error_code", "") == CapabilityErrorCode.CAPABILITY_MISSING.value
    else:  # pragma: no cover
        raise AssertionError("空能力声明被接受")


def test_register_requires_provider_id_for_each_group():
    _reset_leases()
    try:
        _register(
            _client_payload(
                provider_id="",
                capabilities=[],
                providers=[{"provider_id": "", "capabilities": [{"capability": "workspace.read@1"}]}],
            )
        )
    except Exception as exc:  # noqa: BLE001
        assert "provider_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺少 provider_id 的声明被接受")


def test_client_declared_locality_cannot_override_catalog():
    """客户端把本地能力声明成 cloud 也不能绕过（本地性以服务端目录为准）。"""
    _reset_leases()
    payload = _client_payload(
        capabilities=[
            {
                "capability": "workspace.read@1",
                "contract_version": 1,
                "data_locality": "cloud",
            }
        ],
        providers=[],
    )
    response = _register(payload)
    lease = response["data"]["leases"][0]
    # 租约按目录的 local_only 记录，且位置仍是 client
    assert lease["deployment"] == "client"
    assert lease["capability"] == "workspace.read@1"


# ── 撤销通道 ─────────────────────────────────────────────────────


def test_revoked_provider_is_removed_immediately():
    _reset_leases()
    _register(_client_payload())
    assert cap_api.lease_service.leases_for("workspace.read")
    response = _heartbeat(
        {
            "provider_id": "lumi.local.workspace",
            "revoked_providers": [
                {"provider_id": "lumi.local.workspace", "reason": "provider_unregistered"}
            ],
        }
    )
    assert cap_api.lease_service.leases_for("workspace.read") == []
    assert {row["reason"] for row in response["data"]["revoked"]} == {"provider_unregistered"}


def test_revoked_capability_requires_provider_and_removes_only_that_one():
    _reset_leases()
    _register(_client_payload())
    response = _heartbeat(
        {
            "provider_id": "lumi.local.workspace",
            "revoked_capabilities": [
                {
                    "capability": "workspace.write@1",
                    "provider_id": "lumi.local.workspace",
                    "reason": "health_check_failed",
                },
                # 缺 provider_id：必须忽略（否则会跨 Provider 误删）
                {"capability": "workspace.read@1", "reason": "no_provider"},
            ],
        }
    )
    remaining = [lease.capability for lease in cap_api.lease_service.leases_for("workspace.read")]
    assert remaining == ["workspace.read"]
    assert cap_api.lease_service.leases_for("workspace.write") == []
    assert [row["capability"] for row in response["data"]["revoked"]] == ["workspace.write@1"]


def test_heartbeat_with_only_revocations_is_accepted():
    """只上报撤销、不续期也是合法心跳（客户端正在摘除能力）。"""
    _reset_leases()
    _register(_client_payload())
    response = _heartbeat(
        {
            "revoked_providers": [
                {"provider_id": "lumi.local.workspace", "reason": "plugin_uninstalled"}
            ]
        }
    )
    assert response["data"]["leases"] == []
    assert response["data"]["revoked"]


# ── expires_at 语义 ──────────────────────────────────────────────


def test_heartbeat_returns_future_expiry_and_renews():
    _reset_leases()
    first = _register(_client_payload())
    assert first["data"]["expires_at"] > time.time()
    assert first["data"]["renewed"] is True

    # 把租约临期化，再心跳：过期时间必须往后走（客户端据此判断"还在续租"）。
    key = next(iter(cap_api.lease_service._leases))  # noqa: SLF001
    near = time.time() + 5
    cap_api.lease_service._leases[key] = cap_api.lease_service._leases[key].model_copy(  # noqa: SLF001
        update={"expires_at": near}
    )
    renewed = _heartbeat(_client_payload())
    assert renewed["data"]["renewed"] is True
    assert renewed["data"]["expires_at"] > near
    assert renewed["data"]["expires_at"] > time.time() + 100
    assert renewed["data"]["errors"] == []


def test_expired_lease_heartbeat_reports_lease_expired_and_no_future_expiry():
    """过期后心跳必须失败（非 2xx + ``LEASE_EXPIRED``），且**不复活**租约。

    客户端契约：非 2xx 或 ``expires_at`` 为过去时间 → 停止续租；这样它最迟 120s 后
    本机调用返回 ``LEASE_EXPIRED``，并提示用户"重新注册能力"。
    """
    _reset_leases()
    _register(_client_payload())
    for key, lease in list(cap_api.lease_service._leases.items()):  # noqa: SLF001
        cap_api.lease_service._leases[key] = lease.model_copy(  # noqa: SLF001
            update={"expires_at": time.time() - 1}
        )
    try:
        _heartbeat(_client_payload())
    except Exception as exc:  # noqa: BLE001 - BadRequestException
        assert getattr(exc, "error_code", "") == CapabilityErrorCode.LEASE_EXPIRED.value
    else:  # pragma: no cover
        raise AssertionError("过期租约的心跳没有失败（客户端会误以为还在续租）")
    # 服务端也没有把过期租约变成"可用"：过期条目被直接摘除（不会靠心跳复活）。
    assert cap_api.lease_service.leases_for("workspace.read") == []
    assert cap_api.lease_service.snapshot() == []


# ── 路由表与错误码 ───────────────────────────────────────────────


def test_tool_capability_map_matches_client_bridge():
    expected = {
        "workspace_navigator": "workspace.read",
        "workspace_catalog": "workspace.read",
        "workspace_list": "workspace.read",
        "workspace_stat": "workspace.read",
        "workspace_read": "workspace.read",
        "workspace_search": "workspace.read",
        "workspace_content_extract": "workspace.read",
        "workspace_write": "workspace.write",
        "workspace_stage_write": "workspace.write",
        "workspace_stage_delete": "workspace.write",
        "workspace_rollback": "workspace.write",
        "workspace_diff": "git.operations",
        "workspace_commit": "git.operations",
        "sandbox_prepare": "code.execute",
        "sandbox_run": "code.execute",
        "sandbox_output_read": "code.execute",
        "sandbox_reset": "code.execute",
    }
    for tool, capability in expected.items():
        assert capability_for_tool(tool) == capability, tool
    for tool in ("desktop_open_app", "desktop_open_url", "user_clarify"):
        assert capability_for_tool(tool) is None
    assert capability_for_tool("unknown_tool") is None


def test_dispatch_map_endpoint_exposes_the_routing_table():
    response = asyncio.run(cap_api.capability_dispatch_map(payload={"sub": "u1"}))
    data = response["data"]
    assert data["inbound"] == "mcp"
    assert data["new_endpoint_required"] is False
    tools = {row["tool"]: row["capability"] for row in data["tools"]}
    assert tools["workspace_read"] == "workspace.read"
    assert tools["workspace_commit"] == "git.operations"
    assert tools["desktop_open_app"] is None


def test_client_error_codes_all_exist_and_retryability_matches():
    """客户端用的错误码必须全部存在；可重试集合与它的表格一致。"""
    required = {
        "PROVIDER_UNHEALTHY": True,
        "PROVIDER_OFFLINE": True,
        "LEASE_EXPIRED": True,
        "TIMEOUT": True,
        "APPROVAL_REQUIRED": True,
        "CAPABILITY_UNAVAILABLE": False,
        "CAPABILITY_MISSING": False,
        "POLICY_DENIED": False,
        "WORKSPACE_NOT_BOUND": False,
        "WORKSPACE_DEVICE_OFFLINE": False,
        "WORKSPACE_NOT_REGISTERED": False,
        "WORKSPACE_ROOT_MISSING": False,
        "WORKSPACE_PATH_NOT_DIRECTORY": False,
    }
    codes = {str(item) for item in CapabilityErrorCode}
    for code in required:
        assert code in codes, f"客户端在用但后端没有的错误码：{code}"
    for code, retryable in required.items():
        assert (code in RETRYABLE_CAPABILITY_ERRORS) is retryable, code


def test_provider_unhealthy_maps_to_unavailable_display_state():
    from lumi_contracts.plugins import CapabilityStatus

    assert (
        capability_status_for(error_code="PROVIDER_UNHEALTHY")
        is CapabilityStatus.UNAVAILABLE
    )


def test_client_advertised_capabilities_match_catalog_and_locality():
    """客户端广告的 4 个能力：后端目录里存在、都是 local_only、版本为 1。"""
    advertised = {
        "workspace.read@1": (CAPABILITY_WORKSPACE_READ, 30.0),
        "workspace.write@1": (CAPABILITY_WORKSPACE_WRITE, 30.0),
        "code.execute@1": (CAPABILITY_CODE_EXECUTE, 60.0),
        "git.operations@1": (CAPABILITY_GIT_OPERATIONS, 60.0),
    }
    for qualified, (name, _timeout) in advertised.items():
        descriptor = capability_catalog.get(name, version=1)
        assert descriptor is not None, qualified
        assert descriptor.qualified_name == qualified
        assert descriptor.data_locality is DataLocality.LOCAL_ONLY
    # artifact.create 是 cloud：客户端不广告，服务端自己执行
    artifact = capability_catalog.require(CAPABILITY_ARTIFACT_CREATE)
    assert artifact.data_locality is DataLocality.CLOUD
    # code.scan 同样**不在**客户端广告集合里：解析实现是服务端聚合服务
    # （客户端只登记描述，见客户端 CAPABILITY_BRIDGE.md §2.1）。因此它必须是
    # 可回退的只读能力——没有租约时走 workspace_navigator(action=scan) 的同一实现，
    # 否则"客户端不广告 + 不允许回退"会让这个能力永远不可用。
    from app.agents.capabilities.broker.dispatch import NEVER_FALLBACK_CAPABILITIES

    scan = capability_catalog.require(CAPABILITY_CODE_SCAN)
    assert scan.data_locality is DataLocality.LOCAL_ONLY
    assert scan.name not in NEVER_FALLBACK_CAPABILITIES


def test_write_capabilities_declare_local_confirmation_and_approval_side_effects():
    """客户端表里 write/execute/git 需要审批；后端副作用声明必须能推出这一点。"""
    from lumi_contracts.plugins import SideEffectKind

    for name in (CAPABILITY_WORKSPACE_WRITE, CAPABILITY_CODE_EXECUTE, CAPABILITY_GIT_OPERATIONS):
        descriptor = capability_catalog.require(name)
        assert descriptor.needs_local_confirmation is True, name
        assert {str(item) for item in descriptor.side_effects} & {
            SideEffectKind.WRITE.value,
            SideEffectKind.DELETE.value,
            SideEffectKind.EXECUTE.value,
        }, name
    read = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    assert read.needs_local_confirmation is False


def test_health_status_from_client_is_recorded_on_lease():
    _reset_leases()
    payload = _client_payload()
    payload["providers"][0]["health_status"] = ProviderHealth.HEALTHY.value
    response = _register(payload)
    assert all(row["health_status"] == "healthy" for row in response["data"]["leases"])
