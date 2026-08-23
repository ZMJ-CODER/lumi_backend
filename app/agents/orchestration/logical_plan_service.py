"""Continuation service for rolling logical plans.

The service owns the durable-plan/frontier handshake.  It intentionally does
not decide whether a failed frontier should be replanned; that arbitration is
kept in the orchestrator's replan policy path.
"""

from __future__ import annotations

import time

from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import StateStore


class LogicalPlanContinuationService:
    """Commit one materialized frontier and prepare the next execution window."""

    def __init__(self, *, store: StateStore) -> None:
        self._store = store

    async def continue_job(self, job: Job) -> bool:
        """Return ``True`` when the caller should execute a fresh frontier."""
        pointer = (job.routing or {}).get("logical_plan")
        if not isinstance(pointer, dict) or not pointer.get("plan_id"):
            return False
        if job.status in {
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
            JobStatus.WAITING_APPROVAL,
            JobStatus.WAITING_RESOURCES,
        }:
            return False

        from app.agents.orchestration.logical_plan import (
            commit_frontier_results,
            load_logical_plan,
            logical_plan_progress,
            materialize_frontier,
            save_logical_plan,
        )

        plan = await load_logical_plan(job.user_id, str(pointer["plan_id"]))
        if not plan:
            job.status = JobStatus.FAILED
            job.error = "逻辑计划状态不可用，已停止以避免重复执行。"
            await self._store.save_job(job)
            return False

        await commit_frontier_results(job.user_id, plan, job.nodes)
        progress = logical_plan_progress(plan)
        job.routing = dict(job.routing or {})
        job.routing["logical_plan"] = {
            **pointer,
            "revision": plan.get("revision", 1),
            "progress": progress,
            "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
        }
        # A failed materialized node is handed to L2/L3 arbitration.  Do not
        # materialize a tail before the caller decides whether to replace it.
        if progress["failed"]:
            await self._store.save_job(job)
            return False

        if progress["completed"] >= progress["total"]:
            job.status = JobStatus.COMPLETED
            job.error = None
            job.updated_at = time.time()
            await save_logical_plan(job.user_id, plan)
            await self._store.save_job(job)
            return False

        frontier = materialize_frontier(plan)
        if not frontier:
            budget = plan.get("budget") or {}
            if int(budget.get("used_estimated") or 0) + int(
                budget.get("reserved") or 0
            ) >= int(budget.get("limit") or 0):
                job.error = "任务执行预算已用尽，未执行的后续步骤已停止。"
            else:
                job.error = "逻辑计划没有满足依赖的后续步骤，已停止以避免无效调度。"
            job.status = JobStatus.FAILED
            job.updated_at = time.time()
            await save_logical_plan(job.user_id, plan)
            await self._store.save_job(job)
            return False

        await save_logical_plan(job.user_id, plan)
        job.nodes = frontier
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.updated_at = time.time()
        await self._store.save_job(job)
        return True
