"""run_logical_effects_frontier_activity（logical_read_activities 的 frontier_effects 族）。"""

from __future__ import annotations
import asyncio
import time
from temporalio import activity
from app.agents.orchestration.models import JobStatus, TaskStatus
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
async def run_logical_effects_frontier_activity(payload: dict) -> dict:
    """推进一个带预声明审批节点的逻辑计划前沿。

    该路径复用 ``execute_dag`` 的 effect journal、Redis 资源锁和确认指纹。
    它不执行自动重规划：失败写操作应保留人工可审计的终态，而非改变尾部。
    """
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"terminal": True, "status": "failed", "error": "missing_job_id"}
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
    from app.agents.orchestration.runtime.temporal_policy import evaluate_logical_effect_temporal
    from app.agents.orchestration.execution.workers import WORKERS

    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None:
        return {"terminal": True, "status": "failed", "error": "job_not_found"}
    approvals = list((payload or {}).get("approvals") or [])

    def acknowledged_approval() -> dict | None:
        """Return a Signal only after its durable control-plane decision is visible.

        ``JobControlService`` writes the approval result before signaling the
        Workflow.  The Activity therefore treats a signal as a wake-up and
        acknowledgement token, never as authority to approve an arbitrary
        node.  This preserves retry safety if the Signal delivery succeeds
        after the first Activity attempt has already completed.
        """
        for item in reversed(approvals):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or "")
            node = next((candidate for candidate in job.nodes if candidate.id == node_id), None)
            if node is None:
                continue
            metadata = node.metadata or {}
            approved = bool(item.get("approved", True))
            if approved and metadata.get("confirmed_tool_calls"):
                return {"node_id": node_id, "approved": True}
            if not approved and node.status == TaskStatus.SKIPPED and node.error == "用户拒绝审批":
                return {"node_id": node_id, "approved": False}
        return None

    consumed_approval = acknowledged_approval()
    if job.status in _TERMINAL:
        return {
            "terminal": True,
            "status": job.status.value,
            "consumed_approval": consumed_approval,
        }
    pointer = (job.routing or {}).get("logical_plan") or {}
    plan = await load_logical_plan(job.user_id, str(pointer.get("plan_id") or ""))
    decision = evaluate_logical_effect_temporal(job, plan)
    if not decision.eligible:
        job.status = JobStatus.FAILED
        job.error = f"Temporal 副作用逻辑计划准入校验失败: {decision.detail}"
        await store.save_job(job)
        return {"terminal": True, "status": job.status.value, "error": decision.code}
    assert isinstance(plan, dict)

    if job.status == JobStatus.WAITING_APPROVAL:
        # ApprovalService.resolve() is intentionally invoked by the control
        # plane before the Workflow signal is emitted.  An Activity must not
        # turn an unauthenticated/stale signal into a durable approval.
        waiting_node = next(
            (node for node in job.nodes if (node.metadata or {}).get("awaiting_approval")), None
        )
        expires_at = float((waiting_node.metadata or {}).get("approval_expires_at") or 0) if waiting_node else 0
        # A current clock read is allowed here (Activity), not in Workflow.
        wait_seconds = max(1, int(expires_at - time.time())) if expires_at else 60
        return {
            "terminal": False,
            "waiting_approval": True,
            "status": job.status.value,
            "approval_wait_seconds": wait_seconds,
        }

    interval = max(5.0, float(settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS))
    stopped = asyncio.Event()

    async def heartbeat_loop() -> None:
        while not stopped.is_set():
            _heartbeat({"job_id": job_id, "phase": "logical_effects_frontier"})
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
        if job.status == JobStatus.WAITING_APPROVAL:
            # The active frontier remains materialized. The next Activity
            # receives an approval Signal, then re-enters the same node with
            # its immutable confirmation fingerprint and effect idempotency key.
            return {
                "terminal": False,
                "waiting_approval": True,
                "status": job.status.value,
                "consumed_approval": consumed_approval,
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
            job.status = JobStatus.FAILED
            job.error = job.error or "逻辑计划前沿执行失败，副作用路径不自动重规划。"
            await save_logical_plan(job.user_id, plan)
            await store.save_job(job)
            return {
                "terminal": True,
                "status": job.status.value,
                "progress": progress,
                "consumed_approval": consumed_approval,
            }
        if progress["completed"] >= progress["total"]:
            waiting = await _wait_for_ready_expansion(store, job, plan, pointer)
            if waiting is not None:
                waiting["consumed_approval"] = consumed_approval
                return waiting
            job.status = JobStatus.COMPLETED
            job.error = None
            await _finalize_logical_answer(job, plan)
            await save_logical_plan(job.user_id, plan)
            await store.save_job(job)
            return {
                "terminal": True,
                "status": job.status.value,
                "progress": progress,
                "consumed_approval": consumed_approval,
            }
        frontier = materialize_frontier(plan)
        if not frontier:
            job.status = JobStatus.FAILED
            job.error = "逻辑计划没有满足依赖的后续步骤，已停止以避免无效调度。"
            await save_logical_plan(job.user_id, plan)
            await store.save_job(job)
            return {
                "terminal": True,
                "status": job.status.value,
                "progress": progress,
                "consumed_approval": consumed_approval,
            }
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
            "consumed_approval": consumed_approval,
        }
    finally:
        stopped.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
