"""cancel_logical_effects_job_activity fail_logical_read_job_activity（logical_read_activities 的 lifecycle 族）。"""

from __future__ import annotations
import time
from temporalio import activity
from app.agents.orchestration.models import JobStatus, TaskStatus
from app.agents.orchestration.runtime.state import RedisStateStore
from app.agents.orchestration.temporal.logical_read_activities._shared import (
    _TERMINAL,
)


@activity.defn
async def cancel_logical_effects_job_activity(payload: dict) -> dict:
    """Persist cancellation for an effects Workflow without executing nodes."""
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"cancelled": False, "reason": "missing_job_id"}
    keep_completed = bool((payload or {}).get("keep_completed", True))
    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None:
        return {"cancelled": False, "reason": "job_not_found"}
    if job.status in _TERMINAL:
        return {"cancelled": job.status == JobStatus.CANCELLED, "status": job.status.value}
    for node in job.nodes:
        if node.status == TaskStatus.COMPLETED and keep_completed:
            continue
        if node.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RETRYING, TaskStatus.ESCALATED}:
            node.status = TaskStatus.CANCELLED
            node.error = "任务已取消"
            node.error_code = "JOB_CANCELLED"
            node.completed_at = time.time()
    job.status = JobStatus.CANCELLED
    job.error = "任务已取消"
    job.updated_at = time.time()
    await store.save_job(job)
    return {"cancelled": True, "status": job.status.value}


@activity.defn
async def fail_logical_read_job_activity(payload: dict) -> None:
    """Make an exhausted Workflow/Activity failure visible in the Job store."""
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return
    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None or job.status in _TERMINAL | {JobStatus.PAUSED}:
        return
    job.status = JobStatus.FAILED
    job.error = str((payload or {}).get("error") or "逻辑计划前沿执行失败")[:500]
    job.updated_at = time.time()
    await store.save_job(job)
