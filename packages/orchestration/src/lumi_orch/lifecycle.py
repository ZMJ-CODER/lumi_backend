"""编排任务通用的生命周期转换契约。"""

from __future__ import annotations

from typing import Any


ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "waiting_approval", "waiting_resources", "failed", "cancelled", "interrupted"}),
    "running": frozenset({
        "paused", "completed", "failed", "cancelled", "interrupted", "waiting_approval", "waiting_resources", "continuing",
    }),
    "paused": frozenset({"running", "failed", "cancelled", "interrupted"}),
    "waiting_approval": frozenset({"running", "failed", "cancelled", "interrupted"}),
    "waiting_resources": frozenset({"running", "paused", "failed", "cancelled", "interrupted"}),
    "continuing": frozenset({
        "running", "waiting_approval", "waiting_resources", "completed", "failed", "cancelled", "interrupted",
    }),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}


class InvalidStateTransition(ValueError):
    """Raised when a control or runtime operation requests an illegal move."""

    def __init__(self, current: Any, target: Any) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid orchestration state transition: {_value(current)} -> {_value(target)}")


def can_transition(current: Any, target: Any) -> bool:
    """Return whether a lifecycle transition is valid for any string-like enum."""
    current_value = _value(current)
    target_value = _value(target)
    return current_value == target_value or target_value in ALLOWED_TRANSITIONS.get(current_value, frozenset())


def transition(entity: Any, target: Any) -> Any:
    """Validate and apply a transition without persistence or timestamp work."""
    if not can_transition(entity.status, target):
        raise InvalidStateTransition(entity.status, target)
    entity.status = target
    return entity


def _value(status: Any) -> str:
    return str(getattr(status, "value", status))
