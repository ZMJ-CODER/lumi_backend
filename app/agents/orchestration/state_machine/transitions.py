"""Lumi compatibility exports for the kernel lifecycle contract."""

from __future__ import annotations

from lumi_orch.lifecycle import (
    ALLOWED_TRANSITIONS as _KERNEL_ALLOWED_TRANSITIONS,
    InvalidStateTransition,
    can_transition,
    transition,
)

from app.agents.orchestration.models import JobStatus


# Preserve the historical enum-keyed public constant for existing callers.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus(current): frozenset(JobStatus(target) for target in targets)
    for current, targets in _KERNEL_ALLOWED_TRANSITIONS.items()
}

__all__ = ["ALLOWED_TRANSITIONS", "InvalidStateTransition", "can_transition", "transition"]
