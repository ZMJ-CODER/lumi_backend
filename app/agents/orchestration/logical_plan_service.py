"""滚动逻辑计划的续执行服务。

本服务负责持久化计划与执行前沿之间的衔接。它刻意不决定失败前沿是否需要重
规划；该裁决保留在编排器的重规划策略路径中。
"""

from __future__ import annotations

import time

from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import StateStore


class LogicalPlanContinuationService:
    """Commit one materialized frontier and prepare the next execution window."""

    def __init__(self, *, store: StateStore) -> None:
        self._store = store

    @staticmethod
    async def _single_terminal_answer(user_id: str, plan: dict) -> str:
        """Return the sole terminal node's output without copying all plan results.

        A rolling plan keeps completed node bodies outside ``Job.nodes``. Once
        the plan converges, a single terminal node is already the user-facing
        answer, so restore just that referenced result into the job snapshot.
        Multiple terminal branches use the normal final-answer synthesis path.
        """
        records = plan.get("nodes") or {}
        order = [str(node_id) for node_id in (plan.get("order") or [])]
        depended_on = {
            str(dependency)
            for record in records.values()
            if isinstance(record, dict)
            for dependency in ((record.get("node") or {}).get("depends_on") or [])
        }
        terminal_ids = [node_id for node_id in order if node_id not in depended_on]
        if len(terminal_ids) != 1:
            return ""

        record = records.get(terminal_ids[0]) or {}
        if str(record.get("status") or "") != "completed":
            return ""
        from app.agents.orchestration.execution.lineage import resolve_result_ref

        result = await resolve_result_ref(user_id, record.get("result_ref"))
        if not isinstance(result, dict):
            return ""
        return str(result.get("content") or result.get("output") or "").strip()

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
        from app.agents.orchestration.scheduling.plan_patches import ready_slots

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
            slots = ready_slots(plan)
            if slots:
                # 插槽不是普通节点，不能因为当前图已清空就提前完成。使用
                # PAUSED 复用现有恢复控制，但标记来源以区分用户手动暂停。
                job.status = JobStatus.PAUSED
                job.error = None
                job.updated_at = time.time()
                job.routing["scheduler_waiting_slots"] = [slot.id for slot in slots]
                await save_logical_plan(job.user_id, plan)
                await self._store.save_job(job)
                return False
            final_answer = await self._single_terminal_answer(job.user_id, plan)
            if final_answer:
                job.result = {"final_answer": final_answer}
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
