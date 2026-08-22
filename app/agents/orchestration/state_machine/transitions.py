"""合法的 Job 状态转换。

This module is intentionally free of persistence and runtime imports.  It can
therefore be used by the API control plane, legacy runner, and Temporal
adapter without creating a dependency cycle.
"""

from __future__ import annotations

from app.agents.orchestration.models import Job, JobStatus


class InvalidStateTransition(ValueError):
    """Raised when a control or runtime operation requests an illegal move."""

    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"非法任务状态转换: {current.value} -> {target.value}")


# Terminal jobs are immutable from the control plane.  A new execution/fork
# must be created instead of mutating historical state.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset(
        {JobStatus.RUNNING, JobStatus.WAITING_APPROVAL, JobStatus.FAILED,
         JobStatus.CANCELLED, JobStatus.INTERRUPTED}
    ),
    JobStatus.RUNNING: frozenset(
        {JobStatus.PAUSED, JobStatus.COMPLETED, JobStatus.FAILED,
         JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.WAITING_APPROVAL,
         JobStatus.CONTINUING}
    ),
    JobStatus.PAUSED: frozenset(
        {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
    ),
    JobStatus.WAITING_APPROVAL: frozenset(
        {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
    ),
    JobStatus.CONTINUING: frozenset(
        {JobStatus.RUNNING, JobStatus.WAITING_APPROVAL, JobStatus.COMPLETED,
         JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.INTERRUPTED: frozenset(),
}


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    """Return whether ``current -> target`` is a supported state change."""
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(job: Job, target: JobStatus) -> Job:
    """Validate and apply a transition to a Job in place.

    Persistence, timestamps, and audit events remain the caller's concerns.
    """
    if not can_transition(job.status, target):
        raise InvalidStateTransition(job.status, target)
    job.status = target
    return job
