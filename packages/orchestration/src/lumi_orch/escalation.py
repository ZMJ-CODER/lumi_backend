"""Typed escalation protocol between bounded workers and a DAG supervisor."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EscalationLevel(str, Enum):
    TASK = "task"
    PLAN = "plan"


class EscalationReason(str, Enum):
    APPROVAL_REQUIRED = "approval_required"
    MISSING_PREREQUISITE = "missing_prerequisite"
    PRECONDITION_FALSE = "precondition_false"
    CAPABILITY_GAP = "capability_gap"
    PLAN_INVALID = "plan_invalid"


class EscalationSignal(BaseModel):
    """Bounded data signal, never executable instructions."""

    level: EscalationLevel
    reason: EscalationReason
    message: str = Field(default="", max_length=500)
    affected_node_ids: list[str] = Field(default_factory=list, max_length=32)
    preserve_completed: bool = True
    requires_user_input: bool = False
    context: dict[str, Any] = Field(default_factory=dict)


def coerce_escalation(value: Any, *, default_node_id: str = "") -> EscalationSignal | None:
    if not isinstance(value, dict):
        return None
    try:
        signal = EscalationSignal.model_validate(value)
    except Exception:  # noqa: BLE001
        return None
    node_ids = [str(item)[:160] for item in signal.affected_node_ids if str(item).strip()]
    if not node_ids and default_node_id:
        node_ids = [default_node_id]
    return signal.model_copy(update={"affected_node_ids": node_ids})
