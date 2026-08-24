"""Pure retry, terminal-state and escalation decisions."""

from __future__ import annotations

from typing import Any

from lumi_orch.errors import ErrorCategory, ErrorInfo


TERMINAL_JOB_STATUS_VALUES = frozenset({"completed", "failed", "cancelled", "interrupted"})
NON_RETRYABLE_NODE_STATUS_VALUES = frozenset({"cancelled", "interrupted", "skipped"})
ESCALATION_CATEGORY_VALUES = frozenset({
    ErrorCategory.CAPABILITY,
    ErrorCategory.EXTERNAL_SERVICE,
    ErrorCategory.CAPACITY,
    ErrorCategory.TIMEOUT,
})


def status_value(status: Any) -> str:
    return str(getattr(status, "value", status))


def is_terminal(status: Any) -> bool:
    return status_value(status) in TERMINAL_JOB_STATUS_VALUES


def may_retry(node: Any, error: ErrorInfo | None = None) -> bool:
    retries = int(getattr(node, "retries", 0) or 0)
    max_retries = int(getattr(node, "max_retries", 0) or 0)
    if retries >= max_retries or status_value(getattr(node, "status", "")) in NON_RETRYABLE_NODE_STATUS_VALUES:
        return False
    return bool(error and error.retryable) if error is not None else True


def may_replan(error: ErrorInfo | None) -> bool:
    return bool(error and error.replannable)


def may_escalate(error: ErrorInfo | None) -> bool:
    return bool(error and error.category in ESCALATION_CATEGORY_VALUES)
