"""应用侧能力结果入口（定义在 ``lumi_contracts.plugins.capability_result``）。"""

from __future__ import annotations

from lumi_contracts.plugins.capability_result import (
    ERROR_TO_CAPABILITY_STATUS,
    INSTALL_REQUIRED_CAPABILITY_ERRORS,
    RETRYABLE_CAPABILITY_ERRORS,
    CapabilityErrorCode,
    CapabilityResult,
    CapabilityUsage,
    capability_failure,
    capability_ok,
    capability_status_for,
)

__all__ = [
    "ERROR_TO_CAPABILITY_STATUS",
    "INSTALL_REQUIRED_CAPABILITY_ERRORS",
    "RETRYABLE_CAPABILITY_ERRORS",
    "CapabilityErrorCode",
    "CapabilityResult",
    "CapabilityUsage",
    "capability_failure",
    "capability_ok",
    "capability_status_for",
]
