"""Task-level outcome validation and bounded escalation decisions."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.tca import ComplexityLevel, next_level


class FailureCategory(str, Enum):
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
    target_level: ComplexityLevel | None = None


_PARAMETER_CODES = {"INVALID_ARGS", "MISSING_PARAMETER", "VALIDATION_ERROR"}
_CAPABILITY_CODES = {
    "AGENT_NOT_FOUND",
    "SKILL_NOT_FOUND",
    "CAPABILITY_UNAVAILABLE",
    "MODEL_ACTION_REQUIRED",
}
_TRANSIENT_CODES = {"TIMEOUT", "RATE_LIMITED", "NETWORK_ERROR", "SERVICE_UNAVAILABLE"}
_PLAN_CODES = {"DAG_VALIDATION_ERROR", "PLANNING_ERROR", "REVIEW_REJECTED"}
# 这些错误不是规划能力不足，而是确定性产物未被可靠交付。升级到开放式
# LLM 规划会丢失原始文件契约，并诱发无关文档遍历。
_DELIVERY_CODES = {"OUTPUT_MISSING", "ARTIFACT_TRANSFER_FAILED", "SANDBOX_OUTPUT_TRANSFER_FAILED"}


def validate_job_outcome(job: Job) -> ValidationOutcome:
    """Validate the contract of the selected layer without another LLM call."""
    failed = next((node for node in job.nodes if node.status == TaskStatus.FAILED), None)
    if job.status == JobStatus.COMPLETED and job.nodes and all(
        node.status == TaskStatus.COMPLETED for node in job.nodes
    ):
        for node in job.nodes:
            result = node.result or {}
            if result.get("success") is False:
                return ValidationOutcome(
                    valid=False,
                    category=FailureCategory.VALIDATION,
                    reason=str(result.get("error") or "工具返回失败结果"),
                    may_upgrade=True,
                    target_level=next_level(job.routing.get("level", "m2")),
                )
            if job.routing.get("level") == ComplexityLevel.M0.value:
                conversion = node.params.get("conversion")
                if isinstance(conversion, dict):
                    expected = str(conversion.get("output_filename") or "").casefold()
                    outputs = result.get("outputs") or []
                    names = {
                        str(item.get("name") or "").casefold()
                        for item in outputs
                        if isinstance(item, dict)
                    }
                    if not expected or expected not in names:
                        return ValidationOutcome(
                            valid=False,
                            category=FailureCategory.VALIDATION,
                            reason="转换步骤完成但未生成约定的交付文件",
                            may_upgrade=False,
                        )
        return ValidationOutcome(valid=True)

    code = str((failed.error_code if failed else "") or "").upper()
    recovery = (failed.metadata or {}).get("recovery") if failed else {}
    reason = str((failed.error if failed else job.error) or "任务结果未通过验证")
    if code in _DELIVERY_CODES:
        return ValidationOutcome(
            valid=False,
            category=FailureCategory.VALIDATION,
            reason=reason[:500],
            may_upgrade=False,
        )
    if code in _PARAMETER_CODES:
        category = FailureCategory.PARAMETER
    elif code in _CAPABILITY_CODES or (recovery or {}).get("replan_required"):
        category = FailureCategory.CAPABILITY
    elif code in _TRANSIENT_CODES:
        category = FailureCategory.TRANSIENT
    elif code in _PLAN_CODES:
        category = FailureCategory.PLAN
    else:
        category = FailureCategory.VALIDATION

    level = ComplexityLevel(job.routing.get("level", "m2"))
    target = next_level(level)
    may_upgrade = category in {
        FailureCategory.CAPABILITY,
        FailureCategory.PLAN,
        FailureCategory.VALIDATION,
    } and target is not None
    return ValidationOutcome(
        valid=False,
        category=category,
        reason=reason[:500],
        may_upgrade=may_upgrade,
        target_level=target if may_upgrade else None,
    )
