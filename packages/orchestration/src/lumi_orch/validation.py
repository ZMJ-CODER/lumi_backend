"""Backend-neutral validation outcome contract."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class FailureCategory(StrEnum):
    PARAMETER = "parameter_error"
    PLAN = "plan_error"
    CAPABILITY = "capability_error"
    TRANSIENT = "transient_error"
    VALIDATION = "validation_error"
    NONE = "none"


class ValidationOutcome(BaseModel):
    valid: bool
    category: FailureCategory = FailureCategory.NONE
    reason: str = ""
    may_upgrade: bool = False
    target_level: str | None = None
