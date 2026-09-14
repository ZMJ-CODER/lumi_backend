"""阶段 3/5 回归：本地拒止收敛 + 企业级能力审计。

本地拒止（客户端 Local Policy Guard 的否决权）必须做到：

1. 结构化回传：``LOCAL_POLICY_DENIED`` + 原因码 + 指纹，**不是**"工具调用失败"；
2. **不可重试**：本机策略不会因为重试而改变（标成可重试只会反复打扰用户）；
3. 在过程气泡里是**可见的一行**（``kind=system``），只放能力名与错误码，不放参数；
4. 服务端不得把它降级成"换个 Provider 再试"（本机否决优先）。

企业审计（``enterprise_audit``）：

5. 只有该策略包下才落审计，且记录里**没有参数与正文**；
6. 审计日志有界，可导出、可摘要（digest 稳定）。
"""

from __future__ import annotations

import asyncio
import json

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    Deployment,
    ProviderHealth,
    SideEffectKind,
    capability_ok,
)

from app.agents.capabilities.audit.audit import (
    CapabilityAuditLog,
    audit_enabled,
    audit_record,
    capability_audit_log,
    is_local_denial,
    normalize_local_deny_reason,
    process_entry_for_result,
    record_if_audited,
    to_capability_result,
)
from app.agents.capabilities.broker.broker import CapabilityBroker
from app.agents.capabilities.catalog.legacy import (
    CAPABILITY_ARTIFACT_CREATE,
    CAPABILITY_WORKSPACE_READ,
    capability_catalog,
)
from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.registry.registry import CapabilityRegistry
from app.services.capability_lease import CapabilityLeaseService


def _invocation(**overrides) -> CapabilityInvocation:
    payload = {
        "capability": CAPABILITY_WORKSPACE_READ,
        "arguments": {"action": "read", "path": "src/app.py"},
        "request_id": "r1",
        "idempotency_key": "i1",
    }
    payload.update(overrides)
    return CapabilityInvocation(**payload)


def _context(**overrides) -> AgentExecutionContext:
    payload = {"user_id": "u1", "workspace_id": "ws-1", "device_id": "device-1"}
    payload.update(overrides)
    return AgentExecutionContext.from_metadata(**payload)


# ── 本地拒止 ─────────────────────────────────────────────────────


def test_local_deny_becomes_structured_non_retryable_result():
    invocation = _invocation()
    result = to_capability_result(
        invocation,
        provider_id="lumi.local.workspace",
        reason_code="path_outside_workspace",
        reason="目标路径不在工作区内",
    )
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.LOCAL_POLICY_DENIED.value
    # 本机否决不会因为重试而改变。
    assert result.retryable is False
    assert result.needs_install is False
    assert result.error.details["reason_code"] == "path_outside_workspace"
    # 指纹用于定位"被拒的是哪一次调用"（不含参数正文）。
    assert len(result.error.details["fingerprint"]) == 64
    assert "src/app.py" not in json.dumps(result.error.model_dump(mode="json"))
    assert is_local_denial(result) is True


def test_unknown_local_deny_reason_collapses_to_other():
    assert normalize_local_deny_reason("PATH_OUTSIDE_WORKSPACE") == "path_outside_workspace"
    assert normalize_local_deny_reason("随便写的") == "other"
    assert normalize_local_deny_reason("") == "other"
    result = to_capability_result(_invocation(), reason_code="weird_reason")
    assert result.error.details["reason_code"] == "other"
    # 没有给出人类可读原因时也要有可展示的兜底文案。
    assert result.error.message


def test_local_deny_is_visible_as_a_system_process_row():
    result = to_capability_result(
        _invocation(),
        provider_id="lumi.local.workspace",
        reason_code="needs_local_confirmation",
        reason="需要你在本机确认",
    )
    entry = process_entry_for_result(result, capability="workspace.read@1", step_id="s1", job_id="j1")
    assert entry["kind"] == "system"
    assert entry["status"] == "failed"
    assert entry["entry_id"].startswith("capability:workspace.read@1")
    assert entry["step_id"] == "s1"
    assert "本机策略拒绝" in entry["title"] or "本机策略拒绝" in entry["summary"]
    # 过程条目里绝不出现参数值。
    assert "src/app.py" not in json.dumps(entry, ensure_ascii=False)


def test_successful_capability_process_row_is_completed():
    entry = process_entry_for_result(
        capability_ok({"status": "ok"}, capability="workspace.read@1"),
        capability="workspace.read@1",
    )
    assert entry["kind"] == "system"
    assert entry["status"] == "completed"


# ── 企业审计 ─────────────────────────────────────────────────────


def test_audit_only_recorded_for_audited_policy():
    assert audit_enabled("enterprise_audit") is True
    assert audit_enabled("manual_commit") is False
    assert audit_enabled("") is False

    log = CapabilityAuditLog()
    invocation = _invocation()
    result = capability_ok({"status": "ok"}, capability="workspace.read@1", provider_id="p1")
    # 默认包不落审计
    assert record_if_audited(invocation, result, policy_id="manual_commit", log=log) is None
    assert log.rows() == []
    # 企业包落审计
    record = record_if_audited(
        invocation,
        result,
        policy_id="enterprise_audit",
        device_id="device-1",
        workspace_id="ws-1",
        log=log,
    )
    assert record is not None
    assert record.capability == "workspace.read@1"
    assert record.fingerprint and record.policy_id == "enterprise_audit"
    assert record.served_locally is False
    assert log.rows() and log.digest()


def test_audit_record_never_contains_arguments_or_body():
    invocation = _invocation(
        arguments={"action": "read", "path": "C:/secret/creds.txt", "token": "sk-live-abcdef123456"}
    )
    result = capability_ok({"status": "ok"}, capability="workspace.read@1")
    record = audit_record(invocation, result, policy_id="enterprise_audit").to_dict()
    blob = json.dumps(record, ensure_ascii=False)
    for forbidden in ("secret", "creds", "sk-live", "abcdef123456", "arguments", "path"):
        assert forbidden not in blob
    # 但仍然能定位这次调用（指纹 + trace/request id）。
    assert len(record["fingerprint"]) == 64
    assert record["request_id"] == "r1"


def test_audit_log_is_bounded_and_stable():
    log = CapabilityAuditLog(limit=3)
    invocation = _invocation()
    result = capability_ok({"status": "ok"}, capability="workspace.read@1")
    for index in range(5):
        log.append(audit_record(invocation, result, now=float(index)))
    rows = log.rows()
    assert len(rows) == 3
    assert [row.occurred_at for row in rows] == [2.0, 3.0, 4.0]
    assert log.digest() == log.digest()
    log.clear()
    assert log.rows() == []


def test_broker_records_audit_only_under_enterprise_policy():
    """端到端：同一个调用在 manual_commit 下不落审计，在 enterprise_audit 下落。"""
    registry = CapabilityRegistry()
    descriptor = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    provider = _ReadProvider(descriptor)
    leases = CapabilityLeaseService(registry=registry, provider_resolver=lambda _lease: provider)

    async def setup():
        await leases.register(
            provider_id=provider.provider_id,
            capabilities=[{"capability": descriptor.name, "contract_version": 1}],
            user_id="u1",
            device_id="device-1",
            workspace_id="ws-1",
            ttl_seconds=60,
            health_status=ProviderHealth.HEALTHY.value,
        )
        leases.sync_registry()

    asyncio.run(setup())
    broker = CapabilityBroker(registry=registry, leases=leases)
    capability_audit_log.clear()

    default_result = asyncio.run(
        broker.invoke(_invocation(), context=_context(), policy_id="manual_commit")
    )
    assert default_result.ok is True
    assert capability_audit_log.rows() == []

    audited = asyncio.run(
        broker.invoke(
            _invocation(request_id="r2", idempotency_key="i2"),
            context=_context(),
            policy_id="enterprise_audit",
        )
    )
    assert audited.ok is True
    rows = capability_audit_log.rows()
    assert len(rows) == 1
    assert rows[0].policy_id == "enterprise_audit"
    assert rows[0].capability == "workspace.read@1"
    capability_audit_log.clear()


class _ReadProvider:
    def __init__(self, descriptor: CapabilityDescriptor) -> None:
        self._descriptor = descriptor
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return "lumi.local.workspace"

    @property
    def deployment(self) -> Deployment:
        return Deployment.CLIENT

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return (self._descriptor,)

    async def invoke(self, invocation, *, context) -> CapabilityResult:
        self.calls += 1
        return capability_ok(
            {"status": "ok"},
            capability=invocation.qualified_capability,
            provider_id=self.provider_id,
            served_locally=True,
        )


def test_artifact_capability_side_effects_are_write_like():
    """审计/审批依赖副作用声明：产物生成必须被识别为写类。"""
    descriptor = capability_catalog.require(CAPABILITY_ARTIFACT_CREATE)
    assert SideEffectKind.WRITE in descriptor.side_effects
