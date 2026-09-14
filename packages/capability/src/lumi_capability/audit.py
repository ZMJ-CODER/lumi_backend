"""能力审计与本地拒止的**记录结构**（纯数据 + 纯函数）。

从 ``app/agents/capabilities/audit/audit.py`` 抽出（结构重构 P3 第二批）。
这里定义"一次能力调用在审计里长什么样"，以及"客户端本地拒止如何变成结构化结果"。

三条不可动摇的语义：

1. **审计记录绝不含参数与正文**：参数可能含敏感值，指纹足够定位"批准/执行的是哪次调用"；
2. **本地拒止是稳定错误码 + 不重试**：本机策略拒绝不会因为重试而改变，
   标成可重试只会让上层反复打扰用户；
3. **执行来源只写事实**：优先取结果里的实际值，其次调用方给的租约事实，
   两者都没有就留空——**绝不**按 ``deployment`` 猜一个看起来合理的值。

**不在这里**的东西：审计日志的进程内缓冲（``CapabilityAuditLog``）与"要不要审计"的开关
（读 settings）——那些是运行时适配/配置，留在 ``app/agents/capabilities/audit/audit.py``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from lumi_contracts.events.process import ProcessKind, ProcessStatus, sanitize_process_text
from lumi_contracts.plugins import (
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    capability_failure,
    executor_type_for,
)

from lumi_capability.fingerprint import capability_fingerprint

#: 本地拒止的允许原因码（客户端本地策略的稳定词表；未知值收敛为 other）。
LOCAL_DENY_REASONS: tuple[str, ...] = (
    "path_outside_workspace",
    "sensitive_directory",
    "ignored_directory",
    "high_risk_command",
    "needs_local_confirmation",
    "size_limit_exceeded",
    "time_limit_exceeded",
    "policy_disabled",
    "other",
)


def _text(value: Any, *, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _fingerprint(invocation: CapabilityInvocation) -> str:
    return capability_fingerprint(
        invocation.qualified_capability, invocation.arguments, scope=invocation.scope
    )


def normalize_local_deny_reason(value: Any) -> str:
    """未知/空原因码收敛为 ``other``（客户端版本可能比服务端新）。"""
    key = str(value or "").strip().casefold()
    return key if key in LOCAL_DENY_REASONS else "other"


def to_capability_result(
    invocation: CapabilityInvocation,
    *,
    provider_id: str = "",
    reason_code: str = "",
    reason: str = "",
    capability: str = "",
    details: dict[str, Any] | None = None,
) -> CapabilityResult:
    """客户端"本地拒止" → 结构化 ``CapabilityResult``。

    ``retryable=False`` 是刻意的：本机策略拒绝**不会**因为重试而改变。
    """
    payload_details = dict(details or {})
    payload_details["reason_code"] = normalize_local_deny_reason(reason_code)
    payload_details["fingerprint"] = _fingerprint(invocation)
    return capability_failure(
        CapabilityErrorCode.LOCAL_POLICY_DENIED,
        _text(reason) or "本机策略拒绝了这次调用",
        capability=str(capability or invocation.qualified_capability),
        provider_id=str(provider_id or ""),
        retryable=False,
        suggested_action="请在客户端调整本地策略或手动确认后再试",
        details=payload_details,
        request_id=invocation.request_id,
        trace_id=invocation.trace_id,
    )


def is_local_denial(result: CapabilityResult) -> bool:
    """本机否决（``LOCAL_POLICY_DENIED``）与服务端策略拒绝（``POLICY_DENIED``）都算拒止。"""
    return result.error_code in {
        CapabilityErrorCode.LOCAL_POLICY_DENIED.value,
        CapabilityErrorCode.POLICY_DENIED.value,
    }


def process_entry_for_result(
    result: CapabilityResult,
    *,
    capability: str = "",
    step_id: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    """能力结果 → 过程条目（``ProcessLogEntry`` 形状）。

    被拒/失败的能力必须是**可见的一行**（``kind=system``），而不是"步骤默默没动"。
    摘要只放能力名与稳定错误码，不含参数正文。
    """
    name = str(capability or result.capability or "能力调用")
    if result.ok:
        title, summary, status = "能力已完成", f"{name} 已完成", ProcessStatus.COMPLETED
    elif is_local_denial(result):
        title = "被本机策略拒绝"
        summary = (
            f"{name} 被本机策略拒绝（{result.error_code}）："
            f"{_text(result.error.message if result.error else '', limit=120)}"
        )
        status = ProcessStatus.FAILED
    else:
        title = "能力未完成"
        summary = f"{name} 未完成（{result.error_code or 'FAILED'}）"
        status = ProcessStatus.FAILED
    entry_id = f"capability:{capability or result.capability}:{result.request_id or ''}".rstrip(":")
    return {
        "id": entry_id,
        "entry_id": entry_id,
        "kind": str(ProcessKind.SYSTEM),
        "title": sanitize_process_text(title, limit=120),
        "summary": sanitize_process_text(summary, limit=300),
        "status": str(status),
        "step_id": str(step_id or ""),
        "call_id": str(result.request_id or ""),
        "tool_name": name[:80],
        "job_id": str(job_id or result.trace_id or ""),
    }


@dataclass(slots=True)
class CapabilityAuditRecord:
    """一次能力调用的审计记录（**无参数、无正文**）。"""

    capability: str
    provider_id: str = ""
    deployment: str = ""
    #: 实际执行来源（位置 + 运行方式 + 兼容派生值）：审计必须回答"当时谁在执行"。
    execution_plane: str = ""
    runtime_kind: str = ""
    executor_type: str = ""
    device_id: str = ""
    workspace_id: str = ""
    contract_version: int = 1
    policy_id: str = ""
    status: str = ""
    error_code: str = ""
    retryable: bool = False
    served_locally: bool = False
    duration_ms: int = 0
    fingerprint: str = ""
    trace_id: str = ""
    request_id: str = ""
    occurred_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "provider_id": self.provider_id,
            "deployment": self.deployment,
            "execution_plane": self.execution_plane,
            "runtime_kind": self.runtime_kind,
            "executor_type": self.executor_type,
            "device_id": self.device_id,
            "workspace_id": self.workspace_id,
            "contract_version": int(self.contract_version),
            "policy_id": self.policy_id,
            "status": self.status,
            "error_code": self.error_code,
            "retryable": bool(self.retryable),
            "served_locally": bool(self.served_locally),
            "duration_ms": int(self.duration_ms),
            "fingerprint": self.fingerprint,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "occurred_at": self.occurred_at,
        }


def audit_record(
    invocation: CapabilityInvocation,
    result: CapabilityResult,
    *,
    policy_id: str = "",
    device_id: str = "",
    workspace_id: str = "",
    deployment: str = "",
    execution_plane: str = "",
    runtime_kind: str = "",
    now: float | None = None,
) -> CapabilityAuditRecord:
    """由调用与结果生成审计记录。

    执行来源优先取**结果里的实际值**（Broker/派发层回填），其次取调用方给的租约事实；
    两者都没有时留空——绝不按 ``deployment`` 猜一个看起来合理的值。
    """
    plane = str(execution_plane or (result.plane() if result.execution_plane else ""))
    runtime = str(runtime_kind or (result.runtime() if result.runtime_kind else ""))
    return CapabilityAuditRecord(
        capability=str(result.capability or invocation.qualified_capability),
        provider_id=str(result.provider_id or ""),
        deployment=str(deployment or ""),
        execution_plane=plane,
        runtime_kind=runtime,
        executor_type=executor_type_for(plane, runtime) if plane else "",
        device_id=str(device_id or ""),
        workspace_id=str(workspace_id or ""),
        contract_version=int(result.contract_version or invocation.contract_version),
        policy_id=str(policy_id or ""),
        status=str(result.status),
        error_code=result.error_code,
        retryable=result.retryable,
        served_locally=bool(result.served_locally),
        duration_ms=int(result.usage.duration_ms or 0),
        fingerprint=_fingerprint(invocation),
        trace_id=str(result.trace_id or invocation.trace_id or ""),
        request_id=str(result.request_id or invocation.request_id or ""),
        occurred_at=time.time() if now is None else float(now),
    )


__all__ = [
    "LOCAL_DENY_REASONS",
    "CapabilityAuditRecord",
    "audit_record",
    "is_local_denial",
    "normalize_local_deny_reason",
    "process_entry_for_result",
    "to_capability_result",
]
