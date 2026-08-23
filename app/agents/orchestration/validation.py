"""Lumi task validation adapter over the kernel outcome contract."""

from __future__ import annotations

from lumi_orch.validation import FailureCategory, ValidationOutcome as KernelValidationOutcome

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.tca import ComplexityLevel, next_level


ValidationOutcome = KernelValidationOutcome

_PARAMETER_CODES = {"INVALID_ARGS", "MISSING_PARAMETER", "VALIDATION_ERROR"}
_CAPABILITY_CODES = {"AGENT_NOT_FOUND", "SKILL_NOT_FOUND", "CAPABILITY_UNAVAILABLE", "MODEL_ACTION_REQUIRED"}
_TRANSIENT_CODES = {"TIMEOUT", "RATE_LIMITED", "NETWORK_ERROR", "SERVICE_UNAVAILABLE"}
_PLAN_CODES = {"DAG_VALIDATION_ERROR", "PLANNING_ERROR", "REVIEW_REJECTED"}
_DELIVERY_CODES = {"OUTPUT_MISSING", "ARTIFACT_TRANSFER_FAILED", "SANDBOX_OUTPUT_TRANSFER_FAILED"}


def validate_job_outcome(job: Job) -> ValidationOutcome:
    """Validate a selected layer without another model call."""
    failed = next(
        (node for node in job.nodes if node.status in {TaskStatus.FAILED, TaskStatus.ESCALATED}),
        None,
    )
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
                    target_level=next_level(job.routing.get("level", "m2")).value
                    if next_level(job.routing.get("level", "m2")) else None,
                )
            if job.routing.get("level") == ComplexityLevel.M0.value:
                conversion = node.params.get("conversion")
                if isinstance(conversion, dict):
                    expected = str(conversion.get("output_filename") or "").casefold()
                    outputs = result.get("outputs") or []
                    names = {str(item.get("name") or "").casefold() for item in outputs if isinstance(item, dict)}
                    if not expected or expected not in names:
                        return ValidationOutcome(
                            valid=False,
                            category=FailureCategory.VALIDATION,
                            reason="转换步骤完成但未生成约定的交付文件",
                        )
        return ValidationOutcome(valid=True)

    code = str((failed.error_code if failed else "") or "").upper()
    recovery = (failed.metadata or {}).get("recovery") if failed else {}
    reason = str((failed.error if failed else job.error) or "任务结果未通过验证")
    if code in _DELIVERY_CODES:
        return ValidationOutcome(valid=False, category=FailureCategory.VALIDATION, reason=reason[:500])
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

    current = ComplexityLevel(job.routing.get("level", "m2"))
    target = next_level(current)
    may_upgrade = category in {FailureCategory.CAPABILITY, FailureCategory.PLAN, FailureCategory.VALIDATION} and target is not None
    return ValidationOutcome(
        valid=False,
        category=category,
        reason=reason[:500],
        may_upgrade=may_upgrade,
        target_level=target.value if may_upgrade else None,
    )
