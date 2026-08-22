"""Stable error taxonomy for orchestration and monitoring consumers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    PLANNING = "planning"
    VALIDATION = "validation"
    CAPABILITY = "capability"
    EXECUTION = "execution"
    TIMEOUT = "timeout"
    APPROVAL = "approval"
    CANCELLATION = "cancellation"
    STATE_CONFLICT = "state_conflict"
    EXTERNAL_SERVICE = "external_service"
    CAPACITY = "capacity"
    UNKNOWN = "unknown"


class OrchestrationError(RuntimeError):
    """An operational error with a stable code and recovery hints."""

    category: ErrorCategory = ErrorCategory.UNKNOWN
    code = "ORCHESTRATION_ERROR"
    retryable = False
    replannable = False
    user_message = "任务执行失败"

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        self.details = details
        super().__init__(message or self.user_message)


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    category: ErrorCategory
    code: str
    message: str
    user_message: str
    retryable: bool = False
    replannable: bool = False


def classify_error(exc: BaseException) -> ErrorInfo:
    """Convert arbitrary runtime errors into a safe, stable error contract."""
    if isinstance(exc, OrchestrationError):
        return ErrorInfo(
            category=exc.category,
            code=exc.code,
            message=str(exc),
            user_message=exc.user_message,
            retryable=exc.retryable,
            replannable=exc.replannable,
        )
    name = type(exc).__name__.lower()
    category = ErrorCategory.TIMEOUT if "timeout" in name else ErrorCategory.UNKNOWN
    return ErrorInfo(
        category=category,
        code="TIMEOUT" if category == ErrorCategory.TIMEOUT else "UNHANDLED_ERROR",
        message=str(exc)[:500],
        user_message="任务执行超时" if category == ErrorCategory.TIMEOUT else "任务执行失败",
        retryable=category == ErrorCategory.TIMEOUT,
        replannable=category == ErrorCategory.TIMEOUT,
    )
