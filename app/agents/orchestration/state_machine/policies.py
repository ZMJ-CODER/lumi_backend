"""Decision policies shared by orchestration runtimes."""

from __future__ import annotations

from app.agents.orchestration.models import JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.state_machine.errors import ErrorCategory, ErrorInfo

TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
)


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_JOB_STATUSES


def may_retry(node: TaskNode, error: ErrorInfo | None = None) -> bool:
    """Retry only a retryable error while the node budget remains."""
    if node.retries >= node.max_retries or node.status in {
        TaskStatus.CANCELLED, TaskStatus.INTERRUPTED, TaskStatus.SKIPPED
    }:
        return False
    return bool(error and error.retryable) if error is not None else True


def may_replan(error: ErrorInfo | None) -> bool:
    return bool(error and error.replannable)


def may_escalate(error: ErrorInfo | None) -> bool:
    if error is None:
        return False
    return error.category in {
        ErrorCategory.CAPABILITY,
        ErrorCategory.EXTERNAL_SERVICE,
        ErrorCategory.CAPACITY,
        ErrorCategory.TIMEOUT,
    }
