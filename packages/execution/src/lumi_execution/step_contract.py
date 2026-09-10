"""计划式单步执行的运行时无关契约与状态迁移（执行内核）。

本模块不依赖 ``lumi_orch`` 的编排策略/视图模块，也不依赖 app；宿主通过
ports 提供状态读写、准入、节点执行，并注入视图/载荷 builder。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ── 共享词汇表（执行内核拥有；编排包/宿主按需复用）──────────
EXECUTION_JOB_STATES = (
    "planning",
    "waiting_run",
    "running_step",
    "waiting_next",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
)

SSE_EVENT_PLAN_DELTA = "plan_delta"
SSE_EVENT_PLAN_READY = "plan_ready"
SSE_EVENT_DONE = "done"
SSE_EVENT_STEP_STARTED = "step_started"
SSE_EVENT_PROCESS = "process"
SSE_EVENT_TOOL_STARTED = "tool_started"
SSE_EVENT_TOOL_COMPLETED = "tool_completed"
SSE_EVENT_STEP_COMPLETED = "step_completed"
SSE_EVENT_WAITING_NEXT = "waiting_next"
SSE_EVENT_WAITING_APPROVAL = "waiting_approval"
SSE_EVENT_TASK_COMPLETED = "task_completed"
SSE_EVENT_TASK_FAILED = "task_failed"
SSE_EVENTS = frozenset({
    SSE_EVENT_PLAN_DELTA, SSE_EVENT_PLAN_READY, SSE_EVENT_DONE, SSE_EVENT_STEP_STARTED,
    SSE_EVENT_PROCESS, SSE_EVENT_TOOL_STARTED, SSE_EVENT_TOOL_COMPLETED,
    SSE_EVENT_STEP_COMPLETED, SSE_EVENT_WAITING_NEXT, SSE_EVENT_WAITING_APPROVAL,
    SSE_EVENT_TASK_COMPLETED, SSE_EVENT_TASK_FAILED,
})

PENDING_STEP_STATUSES = frozenset({"pending", "running", "waiting_approval"})
TERMINAL_STEP_STATUSES = frozenset({"completed", "failed", "skipped"})


@dataclass(slots=True)
class StepRunState:
    """宿主无关的 Job 步骤运行状态快照。"""

    job_id: str
    user_id: str = ""
    job_status: str = "pending"
    canonical: str = "planning"
    plan_revision: int = 1
    current_step_index: int = 0
    steps: list[dict] = field(default_factory=list)
    seen_keys: list[str] = field(default_factory=list)
    error: str | None = None
    updated_at: float = 0.0
    final_answer: str = ""
    execution_mode: str = "step_confirm"
    plan_text: str = ""

    def routing(self) -> dict:
        return {
            "execution_state": self.canonical,
            "execution_mode": self.execution_mode,
            "plan_revision": int(self.plan_revision),
            "current_step_index": int(self.current_step_index),
            "steps": [dict(step) for step in self.steps],
            "plan_text": self.plan_text,
        }

    def as_job_snapshot(self) -> dict:
        """宿主/编排层投影 run_view 所需的 Job 形状（dict 形式）。"""
        return {
            "job_id": self.job_id,
            "status": self.job_status,
            "routing": self.routing(),
            "result": {"final_answer": self.final_answer},
            "updated_at": self.updated_at or time.time(),
        }


@dataclass(slots=True)
class StepCandidate:
    step: dict
    index: int
    step_id: str
    has_more: bool


@dataclass(slots=True)
class StepOutcome:
    status: str = "completed"
    result_summary: str = ""
    error: str = ""
    error_code: str = ""
    result_ref: dict | None = None
    tool_name: str = ""
    tool_display: str = ""


def locate_next_step(state: StepRunState) -> StepCandidate | None:
    """定位当前可执行的下一步（服务端权威，忽略客户端视图）。"""
    index = max(0, int(state.current_step_index or 0))
    for candidate in range(index, len(state.steps)):
        step = dict(state.steps[candidate])
        if str(step.get("status") or "pending") in PENDING_STEP_STATUSES:
            step_id = str(step.get("id") or "")
            return StepCandidate(
                step=step,
                index=candidate,
                step_id=step_id,
                has_more=candidate < len(state.steps) - 1,
            )
    return None


def apply_step_fields(
    state: StepRunState,
    index: int,
    *,
    status: str = "",
    result_ref: dict | None = None,
    result_summary: str | None = None,
    error: str | None = None,
) -> None:
    """原地更新 routing.steps[index]（JSON-safe）。"""
    steps = [dict(item) for item in state.steps]
    if 0 <= index < len(steps):
        step = dict(steps[index])
        if status:
            step["status"] = status
        if result_ref is not None:
            step["result_ref"] = result_ref
        elif status == "pending":
            step.pop("result_ref", None)
        if result_summary is not None:
            step["result_summary"] = str(result_summary or "")[:400]
        if error is not None:
            step["error"] = str(error or "")[:1000]
        steps[index] = step
    state.steps = steps


def mark_running(state: StepRunState, candidate: StepCandidate) -> None:
    state.canonical = "running_step"
    state.current_step_index = candidate.index
    if state.job_status in {"pending", "waiting_resources"}:
        state.job_status = "running"
    apply_step_fields(state, candidate.index, status="running")


def settle_success(state: StepRunState, candidate: StepCandidate) -> dict:
    """成功一步的 canonical 推进；返回 waiting_next 载荷（仍有后续步骤时）。"""
    steps = state.steps
    remaining = [
        s for s in steps if str(s.get("status") or "pending") in {"pending", "waiting_approval"}
    ]
    next_index = candidate.index + 1
    while next_index < len(steps) and str(steps[next_index].get("status") or "pending") not in {
        "pending", "waiting_approval",
    }:
        next_index += 1
    if remaining:
        state.canonical = "waiting_next"
        state.job_status = "pending"
        state.current_step_index = next_index if next_index < len(steps) else candidate.index + 1
        next_step_id = str(steps[next_index].get("id") or "") if next_index < len(steps) else ""
        return {
            "next_step_id": next_step_id,
            "next_step_index": next_index if next_index < len(steps) else -1,
            "completed_step_id": candidate.step_id,
        }
    state.canonical = "completed"
    state.job_status = "completed"
    state.current_step_index = len(steps)
    return {}


def settle_failure(state: StepRunState, candidate: StepCandidate, error: str, error_code: str) -> None:
    apply_step_fields(
        state, candidate.index, status="failed",
        result_summary=str(error or "")[:400], error=error,
    )
    state.canonical = "failed"
    state.job_status = "failed"
    state.error = str(error or "")[:2000]


def settle_approval(state: StepRunState, candidate: StepCandidate) -> None:
    apply_step_fields(state, candidate.index, status="waiting_approval")
    state.canonical = "waiting_approval"


def revert_to_waiting(state: StepRunState, index: int, idempotency_key: str = "") -> None:
    """容量/资源不可用：回滚到等待态并释放本轮的幂等键占用。"""
    if idempotency_key:
        seen = list(state.seen_keys)
        if seen and seen[-1] == idempotency_key:
            seen.pop()
        state.seen_keys = seen[-50:]
    state.canonical = "waiting_run" if index <= 0 else "waiting_next"
    state.job_status = "pending"
    apply_step_fields(state, index, status="pending")


__all__ = [
    "EXECUTION_JOB_STATES",
    "PENDING_STEP_STATUSES",
    "SSE_EVENTS",
    "SSE_EVENT_DONE",
    "SSE_EVENT_PLAN_DELTA",
    "SSE_EVENT_PLAN_READY",
    "SSE_EVENT_PROCESS",
    "SSE_EVENT_STEP_COMPLETED",
    "SSE_EVENT_STEP_STARTED",
    "SSE_EVENT_TASK_COMPLETED",
    "SSE_EVENT_TASK_FAILED",
    "SSE_EVENT_TOOL_COMPLETED",
    "SSE_EVENT_TOOL_STARTED",
    "SSE_EVENT_WAITING_APPROVAL",
    "SSE_EVENT_WAITING_NEXT",
    "StepCandidate",
    "StepOutcome",
    "StepRunState",
    "TERMINAL_STEP_STATUSES",
    "apply_step_fields",
    "locate_next_step",
    "mark_running",
    "revert_to_waiting",
    "settle_approval",
    "settle_failure",
    "settle_success",
]
