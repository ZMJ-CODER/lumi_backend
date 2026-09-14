"""审计记录结构、本地拒止转换与过程条目的纯函数测试。"""

from __future__ import annotations

from lumi_contracts.plugins import (
    CapabilityErrorCode,
    CapabilityInvocation,
    capability_failure,
    capability_ok,
)

from lumi_capability import audit as a


def _invocation(**overrides) -> CapabilityInvocation:
    payload = {"capability": "workspace.write", "arguments": {"path": "/a"}}
    payload.update(overrides)
    return CapabilityInvocation(**payload)


# ── 1. 原因码归一 ───────────────────────────────────────────


def test_known_reason_codes_pass_through():
    for code in a.LOCAL_DENY_REASONS:
        assert a.normalize_local_deny_reason(code) == code


def test_unknown_reason_code_collapses_to_other():
    """客户端版本可能比服务端新：未知码必须收敛，而不是原样透传进前端。"""
    assert a.normalize_local_deny_reason("brand_new_reason") == "other"
    assert a.normalize_local_deny_reason("") == "other"
    assert a.normalize_local_deny_reason(None) == "other"
    assert a.normalize_local_deny_reason("Path_Outside_Workspace") == "path_outside_workspace"


# ── 2. 拒止 → 结构化结果 ────────────────────────────────────


def test_local_denial_is_structured_and_not_retryable():
    result = a.to_capability_result(
        _invocation(), provider_id="lumi.local.workspace", reason_code="sensitive_directory",
        reason="命中敏感目录",
    )
    assert result.error_code == CapabilityErrorCode.LOCAL_POLICY_DENIED.value
    assert result.retryable is False, "本机否决不因重试而改变"
    assert result.provider_id == "lumi.local.workspace"
    assert result.capability == "workspace.write@1", "没有显式 capability 时用调用的**限定名**"
    assert isinstance(result.error, object)


def test_local_denial_details_carry_reason_code_and_fingerprint_only():
    """详情里只有原因码与指纹——**不含参数**（参数可能含敏感值）。"""
    result = a.to_capability_result(_invocation(arguments={"path": "/secret"}), reason_code="other")
    details = result.error.details or {}
    assert details["reason_code"] == "other"
    assert len(details["fingerprint"]) == 64
    assert "/secret" not in str(details)


def test_empty_reason_gets_a_human_default():
    result = a.to_capability_result(_invocation(), reason_code="")
    assert "本机策略拒绝" in (result.error.message if result.error else "")


def test_is_local_denial_covers_both_denial_codes():
    local = capability_failure(CapabilityErrorCode.LOCAL_POLICY_DENIED, "nope")
    policy = capability_failure(CapabilityErrorCode.POLICY_DENIED, "nope")
    other = capability_failure(CapabilityErrorCode.TIMEOUT, "nope")
    assert a.is_local_denial(local) is True
    assert a.is_local_denial(policy) is True
    assert a.is_local_denial(other) is False


# ── 3. 过程条目 ─────────────────────────────────────────────


def test_success_entry_is_completed():
    entry = a.process_entry_for_result(capability_ok(capability="workspace.read"), capability="workspace.read")
    assert entry["status"] == "completed"
    assert entry["kind"] == "system"
    assert "workspace.read" in entry["summary"]


def test_local_denial_entry_is_visible_and_named():
    """被拒的能力必须是一行可见记录，而不是"步骤默默没动"。"""
    denied = a.to_capability_result(_invocation(), reason_code="high_risk_command", reason="危险命令")
    entry = a.process_entry_for_result(denied, capability="workspace.write", step_id="s1", job_id="j1")
    assert entry["title"] == "被本机策略拒绝"
    assert entry["status"] == "failed"
    assert entry["tool_name"] == "workspace.write"
    assert entry["step_id"] == "s1" and entry["job_id"] == "j1"


def test_failed_entry_uses_stable_error_code():
    failed = capability_failure(CapabilityErrorCode.TIMEOUT, "slow", capability="code.execute")
    entry = a.process_entry_for_result(failed)
    assert entry["status"] == "failed"
    assert "TIMEOUT" in entry["summary"]
    assert entry["summary"].startswith("code.execute")


def test_entry_summary_never_carries_arguments():
    result = a.to_capability_result(_invocation(arguments={"path": "/secret"}), reason_code="other")
    entry = a.process_entry_for_result(result, capability="workspace.write")
    assert "/secret" not in entry["summary"]


# ── 4. 审计记录 ─────────────────────────────────────────────


def test_audit_record_contains_no_arguments():
    record = a.audit_record(
        _invocation(arguments={"path": "/secret", "content": "TOPSECRET"}),
        capability_ok(capability="workspace.write"),
        now=123.0,
    )
    blob = str(record.to_dict())
    assert "/secret" not in blob and "TOPSECRET" not in blob
    assert record.occurred_at == 123.0
    assert len(record.fingerprint) == 64


def test_execution_plane_prefers_result_facts_then_lease_facts():
    """执行来源只写事实：结果 > 调用方给的租约事实 > 留空（绝不按 deployment 猜）。"""
    result = capability_ok(capability="workspace.write", execution_plane="client", runtime_kind="in_process")
    from_result = a.audit_record(_invocation(), result)
    assert from_result.execution_plane == "client"
    assert from_result.executor_type, "有执行位置就必须能派生执行者类型"

    plain = capability_ok(capability="workspace.write")
    from_lease = a.audit_record(_invocation(), plain, execution_plane="server", runtime_kind="in_process")
    assert from_lease.execution_plane == "server"

    unknown = a.audit_record(_invocation(), plain)
    assert unknown.execution_plane == "" and unknown.executor_type == ""


def test_audit_record_falls_back_to_invocation_identifiers():
    result = capability_ok(capability="workspace.write")
    record = a.audit_record(_invocation(request_id="r1", trace_id="t1", contract_version=4), result)
    assert record.request_id == "r1"
    assert record.trace_id == "t1"
    # 契约版本优先取**结果**里的值（它就是这次执行实际遵守的版本）
    assert record.contract_version == 1
    # 结果没带版本时回落到调用声明的版本
    without_version = result.model_copy(update={"contract_version": 0})
    record = a.audit_record(_invocation(contract_version=4), without_version)
    assert record.contract_version == 4


def test_audit_record_uses_result_capability_when_present():
    result = capability_ok(capability="code.execute")
    record = a.audit_record(_invocation(), result)
    assert record.capability == "code.execute"
    assert record.status
