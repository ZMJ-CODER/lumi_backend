"""Application compatibility layer for the standalone escalation protocol."""

from __future__ import annotations

from typing import Any

from lumi_orch.escalation import (
    EscalationLevel,
    EscalationReason,
    EscalationSignal,
    coerce_escalation,
)


def infer_escalation(
    *,
    error_code: str | None,
    recovery: dict[str, Any] | None,
    message: str,
    node_id: str,
) -> EscalationSignal | None:
    """Translate Lumi worker recovery semantics into the kernel protocol."""
    code = str(error_code or "").upper()
    category = str((recovery or {}).get("category") or "")
    if code in {"NEEDS_CONFIRMATION", "FORBIDDEN", "REJECTED"} or category == "permission":
        return EscalationSignal(
            level=EscalationLevel.TASK,
            reason=EscalationReason.APPROVAL_REQUIRED,
            message=message[:500] or "该步骤需要用户确认后才能继续",
            affected_node_ids=[node_id],
            requires_user_input=True,
        )
    if code in {"INVALID_ARGS", "MISSING_PARAMETER", "VALIDATION_ERROR"} or category == "input":
        return EscalationSignal(
            level=EscalationLevel.TASK,
            reason=EscalationReason.MISSING_PREREQUISITE,
            message=message[:500] or "完成该步骤缺少必要信息",
            affected_node_ids=[node_id],
            requires_user_input=True,
        )
    if code == "DOCUMENT_SELECTION_AMBIGUOUS":
        return EscalationSignal(
            level=EscalationLevel.PLAN,
            reason=EscalationReason.CAPABILITY_GAP,
            message=message[:500] or "文档摘要无法唯一定位，需改用受限动态核验。",
            affected_node_ids=[node_id],
        )
    if bool((recovery or {}).get("replan_required")):
        return EscalationSignal(
            level=EscalationLevel.PLAN,
            reason=EscalationReason.CAPABILITY_GAP,
            message=message[:500] or "当前方法无法完成该步骤",
            affected_node_ids=[node_id],
        )
    return None


__all__ = [
    "EscalationLevel",
    "EscalationReason",
    "EscalationSignal",
    "coerce_escalation",
    "infer_escalation",
]
