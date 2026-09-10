"""单步恢复（/jobs/{id}/resume）的前置校验纯层。

校验清单（第 7 点）：
  - Job 归属当前用户；
  - 当前状态允许恢复（waiting_run / waiting_next）；
  - expected_step_id 等于当前步骤；
  - plan_revision 与当前一致；
  - idempotency_key 未执行过（去重）；
  - 工作区仍绑定原设备（可选传入校验）；
  - 当前步骤依赖已完成。

纯逻辑：输入输出 JSON-safe，不碰状态库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

RESUME_ALLOWED_STATES = {"waiting_run", "waiting_next", "running_step"}

RESUME_ERROR_NOT_FOUND = "JOB_NOT_FOUND"
RESUME_ERROR_FORBIDDEN = "JOB_NOT_RESUMABLE"
RESUME_ERROR_STEP_MISMATCH = "STEP_MISMATCH"
RESUME_ERROR_REVISION_MISMATCH = "PLAN_REVISION_MISMATCH"
RESUME_ERROR_IDEMPOTENCY = "IDEMPOTENCY_DUPLICATE"
RESUME_ERROR_WORKSPACE_REBOUND = "WORKSPACE_REBOUND"
RESUME_ERROR_DEPENDENCIES = "STEP_DEPENDENCIES_NOT_MET"


@dataclass(frozen=True, slots=True)
class ResumeCheckInput:
    user_id: str
    job_owner: str
    job_state: str                 # canonical 执行状态
    current_step_id: str = ""
    expected_step_id: str = ""
    plan_revision: int = 1
    current_revision: int = 1
    idempotency_key: str = ""
    seen_keys: tuple[str, ...] = ()
    workspace_bound: bool = True   # 当前仍绑定原设备（调用方已核验）
    dependencies_done: bool = True


@dataclass(frozen=True, slots=True)
class ResumeCheckResult:
    allowed: bool
    error_code: str = ""
    reason: str = ""


def validate_resume_request(inputs: ResumeCheckInput) -> ResumeCheckResult:
    if str(inputs.user_id or "").strip() != str(inputs.job_owner or "").strip():
        return ResumeCheckResult(False, RESUME_ERROR_FORBIDDEN, "任务不属于当前用户")
    state = str(inputs.job_state or "").strip()
    if state in {"completed", "failed", "cancelled"}:
        return ResumeCheckResult(False, RESUME_ERROR_FORBIDDEN, f"任务已处于终态（{state}）")
    if state not in RESUME_ALLOWED_STATES:
        return ResumeCheckResult(False, RESUME_ERROR_FORBIDDEN, f"当前状态不允许恢复: {state}")
    expected = str(inputs.expected_step_id or "").strip()
    current = str(inputs.current_step_id or "").strip()
    if expected and expected != current:
        return ResumeCheckResult(False, RESUME_ERROR_STEP_MISMATCH,
                                 f"期望步骤 {expected} 不等于当前步骤 {current}")
    if inputs.plan_revision != inputs.current_revision:
        return ResumeCheckResult(False, RESUME_ERROR_REVISION_MISMATCH,
                                 "plan_revision 与任务不一致")
    key = str(inputs.idempotency_key or "").strip()
    if not key:
        return ResumeCheckResult(False, RESUME_ERROR_IDEMPOTENCY, "缺少 idempotency_key")
    if key in set(inputs.seen_keys):
        return ResumeCheckResult(False, RESUME_ERROR_IDEMPOTENCY, "该 idempotency_key 已执行过")
    if not inputs.workspace_bound:
        return ResumeCheckResult(False, RESUME_ERROR_WORKSPACE_REBOUND,
                                 "工作区已不再绑定原设备")
    if not inputs.dependencies_done:
        return ResumeCheckResult(False, RESUME_ERROR_DEPENDENCIES,
                                 "当前步骤的上游依赖尚未完成")
    return ResumeCheckResult(True)
