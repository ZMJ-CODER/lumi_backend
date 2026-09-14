"""run_logical_read_frontier_activity（logical_read_activities 的 frontier_read 族）。"""

from __future__ import annotations
import asyncio
import time
from temporalio import activity
from app.agents.orchestration.models import JobStatus
from app.agents.orchestration.runtime.state import RedisStateStore
from app.core.config import settings
from app.agents.orchestration.temporal.logical_read_activities._shared import (
    _TERMINAL,
    _heartbeat,
)
from app.agents.orchestration.temporal.logical_read_activities.helpers import (
    _finalize_logical_answer,
    _wait_for_ready_expansion,
)


@activity.defn
async def run_logical_read_frontier_activity(payload: dict) -> dict:
    """执行并提交恰好一个已持久化的纯读逻辑前沿。"""
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"terminal": True, "status": "failed", "error": "missing job_id"}

    from app.agents.orchestration.execution.validation import execute_dag
    from app.agents.orchestration.planning.logical_plan import (
        commit_frontier_results,
        load_logical_plan,
        logical_plan_progress,
        materialize_frontier,
        save_logical_plan,
    )
    from app.agents.orchestration.execution.review import get_reviewer
    from app.agents.orchestration.temporal.client import load_job_llm_config
    from app.agents.orchestration.runtime.temporal_policy import evaluate_logical_read_temporal
    from app.agents.orchestration.execution.workers import WORKERS

    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None:
        return {"terminal": True, "status": "failed", "error": "job_not_found"}
    if job.status in _TERMINAL:
        return {"terminal": True, "status": job.status.value}
    if job.status == JobStatus.PAUSED:
        return {"terminal": False, "paused": True, "status": job.status.value}

    pointer = (job.routing or {}).get("logical_plan") or {}
    plan = await load_logical_plan(job.user_id, str(pointer.get("plan_id") or ""))
    decision = evaluate_logical_read_temporal(job, plan)
    if not decision.eligible:
        job.status = JobStatus.FAILED
        job.error = f"Temporal 纯读逻辑计划准入校验失败: {decision.detail}"
        job.routing = {
            **(job.routing or {}),
            "temporal_logical_read_eligibility": {
                "eligible": False,
                "code": decision.code,
                "detail": decision.detail,
            },
        }
        await store.save_job(job)
        return {"terminal": True, "status": job.status.value, "error": decision.code}
    assert isinstance(plan, dict)

    interval = max(5.0, float(settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS))
    stopped = asyncio.Event()

    async def heartbeat_loop() -> None:
        while not stopped.is_set():
            _heartbeat({"job_id": job_id, "phase": "logical_read_frontier"})
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
            except TimeoutError:
                pass

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        llm_config = await load_job_llm_config(job_id)
        await execute_dag(
            job,
            WORKERS,
            get_reviewer(),
            store,
            concurrency=settings.AGENT_NODE_CONCURRENCY,
            llm_api_key=(llm_config or {}).get("api_key"),
            llm_config=llm_config,
        )
        job = await store.get_job(job_id) or job
        # ``execute_dag`` only records L2 escalation. The Legacy outer loop
        # normally materializes the approval gate afterwards; this Activity
        # is that equivalent control boundary for the Temporal write path.
        if job.status == JobStatus.FAILED:
            from app.agents.orchestration.execution.escalation_service import EscalationService

            await EscalationService(store=store).handle_task_escalation(job)
            job = await store.get_job(job_id) or job
        if job.status in {JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.PAUSED}:
            return {
                "terminal": job.status in {JobStatus.CANCELLED, JobStatus.INTERRUPTED},
                "paused": job.status == JobStatus.PAUSED,
                "status": job.status.value,
            }

        await commit_frontier_results(job.user_id, plan, job.nodes)
        progress = logical_plan_progress(plan)
        job.routing = dict(job.routing or {})
        job.routing["logical_plan"] = {
            **pointer,
            "revision": plan.get("revision", 1),
            "progress": progress,
            "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
        }
        if progress["failed"]:
            # This activity itself has a Temporal retry envelope. Never call a
            # model here: an Activity retry could create a different tail. The
            # Workflow schedules the one-shot replan Activity after this
            # committed result is visible in Redis.
            job.status = JobStatus.FAILED
            job.error = job.error or "纯读逻辑计划前沿执行失败，等待受限替代计划裁决。"
            job.updated_at = time.time()
            await save_logical_plan(job.user_id, plan)
            await store.save_job(job)
            return {
                "terminal": False,
                "replan_required": True,
                "status": job.status.value,
                "progress": progress,
            }
        if progress["completed"] >= progress["total"]:
            waiting = await _wait_for_ready_expansion(store, job, plan, pointer)
            if waiting is not None:
                return waiting
            job.status = JobStatus.COMPLETED
            job.error = None
            await _finalize_logical_answer(job, plan)
            job.updated_at = time.time()
            await save_logical_plan(job.user_id, plan)
            await store.save_job(job)
            return {"terminal": True, "status": job.status.value, "progress": progress}

        frontier = materialize_frontier(plan)
        if not frontier:
            budget = plan.get("budget") or {}
            job.status = JobStatus.FAILED
            job.error = (
                "任务执行预算已用尽，未执行的后续步骤已停止。"
                if int(budget.get("used_estimated") or 0) + int(budget.get("reserved") or 0)
                >= int(budget.get("limit") or 0)
                else "逻辑计划没有满足依赖的后续步骤，已停止以避免无效调度。"
            )
            job.updated_at = time.time()
            await save_logical_plan(job.user_id, plan)
            await store.save_job(job)
            return {"terminal": True, "status": job.status.value, "progress": progress}
        await save_logical_plan(job.user_id, plan)
        job.nodes = frontier
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.updated_at = time.time()
        await store.save_job(job)
        return {
            "terminal": False,
            "status": job.status.value,
            "progress": progress,
            "frontier_size": len(frontier),
        }
    finally:
        stopped.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
