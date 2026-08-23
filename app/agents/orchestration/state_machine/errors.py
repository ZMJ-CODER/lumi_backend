"""Lumi presentation adapter for the kernel error taxonomy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lumi_orch.errors import (
    ErrorCategory,
    ErrorInfo,
    OrchestrationError as KernelOrchestrationError,
    classify_error as classify_kernel_error,
)


class OrchestrationError(KernelOrchestrationError):
    """Keeps the existing Lumi default message while reusing kernel metadata."""

    user_message = "任务执行失败"

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        super().__init__(message, details=details)


def classify_error(exc: BaseException) -> ErrorInfo:
    info = classify_kernel_error(exc)
    if isinstance(exc, KernelOrchestrationError):
        return info
    if info.category == ErrorCategory.TIMEOUT:
        return replace(info, user_message="任务执行超时")
    return replace(info, user_message="任务执行失败")


__all__ = ["ErrorCategory", "ErrorInfo", "OrchestrationError", "classify_error"]
