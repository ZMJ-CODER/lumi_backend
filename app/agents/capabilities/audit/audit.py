"""阶段 3 后端：客户端本地拒止与结构化回传，以及企业级能力审计。

两件事：

1. **本地拒止收敛**（``to_capability_result``）：客户端 ``Local Policy Guard``
   拒绝一次能力调用时，回传的是**结构化拒止**（``LOCAL_POLICY_DENIED`` +
   原因码/人类可读原因 + 可选指纹），而不是一句"工具调用失败"。
   服务端必须：
   * 保留稳定错误码，**不重试、不降级到云端**（本机否决权优先）；
   * 把它变成能力状态事件（``denied`` 展示态）与过程条目（``kind=system``），
     前端在同一气泡里显示"已被本机策略拒绝"。

2. **企业级审计**（``CapabilityAuditLog`` + ``enterprise_audit`` 包）：
   "全量审计"不能只是包里的一个 bool。这里给出**可落库的结构化审计记录**：
   能力名/Provider/设备/契约版本/策略包/耗时/错误码/是否本地执行 + 指纹，
   **绝不含参数与正文**（参数可能含敏感值，指纹已足够定位"批准的是哪次调用"）。

P3 第二批把**纯部分**迁到了 backend-neutral 的 ``lumi_capability.audit`` 与
``lumi_capability.fingerprint``：记录结构（``CapabilityAuditRecord`` / ``audit_record``）、
拒止转换（``to_capability_result`` / ``is_local_denial`` / ``normalize_local_deny_reason``）、
过程条目（``process_entry_for_result``）、指纹（``capability_fingerprint``）。

本模块留下的都是**运行时适配**：进程内环形缓冲（``CapabilityAuditLog``）与
"这个策略包要不要审计"（``audit_enabled``，读策略包配置）。纯部分的公开名在此
**原样再导出**——既有调用点（``from ...audit.audit import audit_record``）不变。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from lumi_contracts.plugins import (
    CapabilityInvocation,
    CapabilityResult,
)

from lumi_capability.audit import (
    LOCAL_DENY_REASONS,
    CapabilityAuditRecord,
    audit_record,
    is_local_denial,
    normalize_local_deny_reason,
    process_entry_for_result,
    to_capability_result,
)

#: 审计环形缓冲上限（进程内；落库位置由调用方决定）。
AUDIT_LOG_MAX_ENTRIES = 500


class CapabilityAuditLog:
    """进程内能力审计日志（有界；供 API/审计导出读取）。"""

    def __init__(self, *, limit: int = AUDIT_LOG_MAX_ENTRIES) -> None:
        self._limit = max(1, int(limit))
        self._rows: list[CapabilityAuditRecord] = []

    def append(self, record: CapabilityAuditRecord) -> CapabilityAuditRecord:
        self._rows.append(record)
        if len(self._rows) > self._limit:
            del self._rows[: len(self._rows) - self._limit]
        return record

    def rows(self) -> list[CapabilityAuditRecord]:
        return list(self._rows)

    def to_snapshot(self) -> list[dict[str, Any]]:
        return [row.to_dict() for row in self._rows]

    def digest(self) -> str:
        blob = json.dumps(self.to_snapshot(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def clear(self) -> None:
        self._rows.clear()


#: 进程内共享审计日志（``enterprise_audit`` 包下每次调用都进这里）。
capability_audit_log = CapabilityAuditLog()


def audit_enabled(policy_id: str = "") -> bool:
    """该策略包是否要求全量审计（``enterprise_audit``）。"""
    try:
        from app.agents.capabilities.policy.policy_packs import policy_packs

        pack = policy_packs.get(policy_id)
        return bool(pack and pack.audit_all)
    except Exception:  # noqa: BLE001 - 配置不可用时按不审计处理
        return False


def record_if_audited(
    invocation: CapabilityInvocation,
    result: CapabilityResult,
    *,
    policy_id: str = "",
    device_id: str = "",
    workspace_id: str = "",
    deployment: str = "",
    log: CapabilityAuditLog | None = None,
) -> CapabilityAuditRecord | None:
    """只在策略要求时落审计（默认包不落，避免无意义的内存增长）。"""
    if not audit_enabled(policy_id):
        return None
    return (log or capability_audit_log).append(
        audit_record(
            invocation,
            result,
            policy_id=policy_id,
            device_id=device_id,
            workspace_id=workspace_id,
            deployment=deployment,
        )
    )


__all__ = [
    "AUDIT_LOG_MAX_ENTRIES",
    "CapabilityAuditLog",
    "CapabilityAuditRecord",
    "LOCAL_DENY_REASONS",
    "audit_enabled",
    "audit_record",
    "capability_audit_log",
    "is_local_denial",
    "normalize_local_deny_reason",
    "process_entry_for_result",
    "record_if_audited",
    "to_capability_result",
]
