"""expire_logical_effects_approval_activity（logical_read_activities 的 approval 族）。"""

from __future__ import annotations
from temporalio import activity
from app.agents.orchestration.runtime.state import RedisStateStore


@activity.defn
async def expire_logical_effects_approval_activity(payload: dict) -> dict:
    """Atomically expire an unattended logical-effects approval gate.

    The Temporal timer is only a wake-up mechanism.  Expiry is always checked
    against the persisted Job in an Activity so a late user Signal cannot be
    mistaken for an expired gate, and a timer replay never reads wall clock.
    """
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"expired": False, "reason": "missing_job_id"}
    from app.agents.orchestration.execution.approval_service import ApprovalService

    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None:
        return {"expired": False, "reason": "job_not_found"}
    expired = await ApprovalService(store=store).expire_if_due(job)
    job = await store.get_job(job_id) or job
    return {"expired": expired, "status": job.status.value}
