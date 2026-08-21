"""Typed boundary between an atomic worker and the office orchestrator.

Workers may explore and recover inside their own bounded LangGraph loop, but
they never mutate the outer DAG.  When a local loop proves that a prerequisite,
approval, or plan assumption is wrong it returns this JSON-safe signal.  The
orchestrator is the sole component allowed to decide whether to pause, clarify,
or mount a replacement subgraph.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EscalationLevel(str, Enum):
    TASK = "task"  # L2: prebuilt control decision, no new open-ended plan
    PLAN = "plan"  # L3: planner must construct a validated replacement graph


class EscalationReason(str, Enum):
    APPROVAL_REQUIRED = "approval_required"
    MISSING_PREREQUISITE = "missing_prerequisite"
    PRECONDITION_FALSE = "precondition_false"
    CAPABILITY_GAP = "capability_gap"
    PLAN_INVALID = "plan_invalid"


class EscalationSignal(BaseModel):
    """A bounded, auditable request for orchestration—not an instruction."""

    level: EscalationLevel
    reason: EscalationReason
    message: str = Field(default="", max_length=500)
    affected_node_ids: list[str] = Field(default_factory=list, max_length=32)
    preserve_completed: bool = True
    requires_user_input: bool = False
    # Evidence is deliberately compact and JSON-only. It is never treated as
    # executable instructions when included in a later planner prompt.
    context: dict[str, Any] = Field(default_factory=dict)


def coerce_escalation(value: Any, *, default_node_id: str = "") -> EscalationSignal | None:
    """Validate untrusted worker output and bind an empty scope to its node."""
    if not isinstance(value, dict):
        return None
    try:
        signal = EscalationSignal.model_validate(value)
    except Exception:  # noqa: BLE001
        return None
    ids = [str(item)[:160] for item in signal.affected_node_ids if str(item).strip()]
    if not ids and default_node_id:
        ids = [default_node_id]
    return signal.model_copy(update={"affected_node_ids": ids})


def infer_escalation(
    *,
    error_code: str | None,
    recovery: dict[str, Any] | None,
    message: str,
    node_id: str,
) -> EscalationSignal | None:
    """Translate existing stable recovery semantics into the new protocol.

    This compatibility bridge means every legacy Skill need not be rewritten at
    once. Explicit worker-provided ``escalation`` still wins over this mapping.
    """
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
    if bool((recovery or {}).get("replan_required")):
        return EscalationSignal(
            level=EscalationLevel.PLAN,
            reason=EscalationReason.CAPABILITY_GAP,
            message=message[:500] or "当前方法无法完成该步骤",
            affected_node_ids=[node_id],
        )
    return None
