"""Temporal 纯读滚动逻辑计划运行时的 Activity。

Workflow 只负责生命周期和 Continue-As-New。本模块是可访问 Redis、执行节点、
解析结果引用并更新可见 Job 快照的边界。这里绝不调用逻辑计划重规划器：变更尾部
必须作为新的、经过明确审核的 Job 提交。
"""

from __future__ import annotations

import asyncio
import time

from temporalio import activity

from app.agents.orchestration.models import JobStatus, TaskStatus
from app.agents.orchestration.state import RedisStateStore
from app.core.config import settings


def _heartbeat(details: dict) -> None:
    try:
        activity.heartbeat(details)
    except RuntimeError:
        # 单元测试可能在 Activity 上下文之外直接调用实现。
        pass


_TERMINAL = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.INTERRUPTED,
}


async def _try_replan_pure_read_tail(job, plan: dict, pointer: dict) -> tuple[bool, str]:
    """由 Activity 进行一次受限 LLM 重规划，成功后挂载新的纯读尾部。

    这是 Temporal Activity 而非 Workflow 的工作：其中会读取 Redis 上下文、调用
    Planner 并执行编译校验。调用方以单次 Activity 执行保障同一失败前沿不会被
    Temporal 重试多次规划。
    """
    from app.agents.orchestration.execution.validation import validate_planned_dag
    from app.agents.orchestration.logical_plan import (
        logical_plan_progress,
        materialize_frontier,
        replace_unfinished_tail,
        save_logical_plan,
    )
    from app.agents.orchestration.plan_compiler import CompileDecision, compile_plan
    from app.agents.orchestration.planning.context import PlanRequestContext
    from app.agents.orchestration.planner import LlmPlanner
    from app.agents.orchestration.replan_evidence_service import ReplanEvidenceService
    from app.agents.orchestration.tca import ComplexityLevel
    from app.agents.orchestration.temporal.client import (
        load_job_llm_config,
        load_temporal_replan_context,
    )
    from app.agents.orchestration.temporal_policy import evaluate_logical_read_temporal
    from app.agents.orchestration.workers import WORKERS

    replan_count = int((job.routing or {}).get("replan_count") or 0)
    if replan_count >= max(0, int(settings.TEMPORAL_LOGICAL_READ_MAX_REPLANS)):
        return False, "replan_limit_reached"
    # A pure-read runtime must never inherit an ambiguous write status from a
    # corrupted/migrated plan, even if current node definitions look safe.
    if any(str((record or {}).get("effect_status") or "") for record in (plan.get("nodes") or {}).values()):
        return False, "effect_status_present"
    llm_config = await load_job_llm_config(job.job_id)
    if not llm_config:
        return False, "llm_context_unavailable"
    replan_context = await load_temporal_replan_context(job.job_id)
    if not replan_context:
        return False, "replan_context_unavailable"
    failed_evidence, evolution_context = await ReplanEvidenceService().logical_plan_context(
        user_id=job.user_id,
        plan=plan,
        prior_summaries="",
    )
    if not failed_evidence:
        return False, "failed_evidence_missing"
    planner = LlmPlanner()
    context = PlanRequestContext(
        user_id=str(replan_context.get("user_id") or job.user_id),
        request=str(replan_context.get("request") or job.request),
        scene=str(replan_context.get("scene") or job.scene),
        project_id=replan_context.get("project_id"),
        project_ids=tuple(replan_context.get("project_ids") or ()),
        llm_api_key=str(llm_config.get("api_key") or "") or None,
        llm_config=llm_config,
        office_docs=tuple(replan_context.get("office_docs") or ()),
        prior_summaries=evolution_context,
    )
    tree = await planner.plan_for_level(
        ComplexityLevel.M3,
        context=context,
        bypass_fast_paths=True,
    )
    if tree.error or not tree.nodes:
        return False, "replacement_empty"
    from app.agents.orchestration.planning.compilation import PlanCompilationService

    compiler_service = PlanCompilationService(
        workers=WORKERS,
        plan_with_context=lambda _context: planner.plan_context(_context),
    )
    compiler_service.normalize_for_replan(
        tree.nodes,
        job.request,
        preserve_dependencies=False,
        adapt_workers=True,
    )
    next_revision = int(plan.get("revision") or 1) + 1
    for node in tree.nodes:
        node.metadata = {**(node.metadata or {}), "plan_revision": next_revision}
        from app.agents.orchestration.safety import prepare_node_safety

        prepare_node_safety(node, job.user_id, job.job_id)
    compiled = await compile_plan(
        tree.nodes,
        scene="office",
        user_role=job.user_role,
        user_id=job.user_id,
        workers=WORKERS,
    )
    if compiled.decision == CompileDecision.REPLAN_REQUIRED:
        return False, "replacement_compile_rejected"
    tree.nodes = compiled.nodes
    if validate_planned_dag(tree.nodes, WORKERS):
        return False, "replacement_dag_invalid"

    # Verify the replacement as a complete plan before mutating the existing
    # one. ``replace_unfinished_tail`` will re-seal the final plan fingerprint.
    probe = {
        "version": plan.get("version", 1),
        "plan_id": plan.get("plan_id"),
        "nodes": {
            node.id: {
                "node": node.model_dump(mode="json"),
                "status": "pending",
                "estimated_tokens": 0,
                "result_ref": None,
                "error": "",
                "error_code": "",
                "effect_status": None,
            }
            for node in tree.nodes
        },
        "order": [node.id for node in tree.nodes],
        "budget": {"limit": 1, "reserved": 0, "used_estimated": 0},
        "revision": next_revision,
        "history": [{"runtime": "temporal_logical_read"}],
    }
    from app.agents.orchestration.logical_plan import logical_plan_execution_fingerprint

    probe["execution_fingerprint"] = logical_plan_execution_fingerprint(probe)
    probe_job = job.model_copy(deep=True)
    probe_job.routing = {"logical_plan": {"plan_id": plan.get("plan_id")}}
    if not evaluate_logical_read_temporal(probe_job, probe).eligible:
        return False, "replacement_not_pure_read"
    reason = "纯读前沿执行失败，已通过受限替代计划更换未完成步骤。"
    replace_unfinished_tail(
        plan,
        tree.nodes,
        reason=reason,
        history_metadata={"runtime": "temporal_logical_read", "replan_count": replan_count + 1},
    )
    frontier = materialize_frontier(plan)
    if not frontier:
        return False, "replacement_frontier_empty"
    job.nodes = frontier
    job.plan_text = tree.plan_text
    job.status = JobStatus.RUNNING
    job.error = None
    job.result = None
    job.updated_at = time.time()
    job.routing = {
        **(job.routing or {}),
        "replan_count": replan_count + 1,
        "plan_revision": plan.get("revision"),
        "plan_change_reason": reason,
        "logical_plan": {
            **pointer,
            "revision": plan.get("revision"),
            "frontier_size": len(frontier),
            "progress": logical_plan_progress(plan),
            "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
        },
    }
    await save_logical_plan(job.user_id, plan)
    return True, "replanned"


async def _finalize_logical_answer(job, plan: dict) -> None:
    """从不透明的逻辑计划结果引用生成最终交付。"""
    if job.result:
        return
    records = plan.get("nodes") or {}
    sources = []
    for node_id in plan.get("order") or []:
        record = records.get(node_id) or {}
        if str(record.get("status") or "") != "completed":
            continue
        node = record.get("node") or {}
        ref = record.get("result_ref")
        if isinstance(ref, dict):
            sources.append(
                {
                    "agent": str(node.get("agent") or ""),
                    "title": str(node.get("name") or node.get("agent") or node_id),
                    "result_ref": ref,
                }
            )
    if len(sources) == 1:
        # 单节点结果可由普通响应路径解析并渲染，无需额外调用 LLM 改写。
        return
    if not sources:
        return
    from app.agents.orchestration.temporal.activities import synthesize_final_answer_activity

    out = await synthesize_final_answer_activity(
        {
            "user_id": job.user_id,
            "job_id": job.job_id,
            "request": job.request,
            "nodes": sources,
        }
    )
    if isinstance(out, dict) and out.get("final_answer"):
        job.result = out


async def _wait_for_ready_expansion(store, job, plan: dict, pointer: dict) -> dict | None:
    """将已完成当前节点图但仍有就绪插槽的任务转为调度等待态。"""
    from app.agents.orchestration.logical_plan import logical_plan_progress, save_logical_plan
    from app.agents.orchestration.scheduling.plan_patches import ready_slots

    slots = ready_slots(plan)
    if not slots:
        return None
    job.status = JobStatus.PAUSED
    job.error = None
    job.updated_at = time.time()
    job.routing = {
        **(job.routing or {}),
        "logical_plan": {
            **pointer,
            "revision": plan.get("revision", 1),
            "progress": logical_plan_progress(plan),
            "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
        },
        "scheduler_waiting_slots": [slot.id for slot in slots],
    }
    await save_logical_plan(job.user_id, plan)
    await store.save_job(job)
    return {
        "terminal": False,
        "waiting_expansion": True,
        "status": job.status.value,
        "slot_ids": [slot.id for slot in slots],
    }


@activity.defn
async def run_logical_read_frontier_activity(payload: dict) -> dict:
    """执行并提交恰好一个已持久化的纯读逻辑前沿。"""
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"terminal": True, "status": "failed", "error": "missing job_id"}

    from app.agents.orchestration.execution.validation import execute_dag
    from app.agents.orchestration.logical_plan import (
        commit_frontier_results,
        load_logical_plan,
        logical_plan_progress,
        materialize_frontier,
        save_logical_plan,
    )
    from app.agents.orchestration.review import get_reviewer
    from app.agents.orchestration.temporal.client import load_job_llm_config
    from app.agents.orchestration.temporal_policy import evaluate_logical_read_temporal
    from app.agents.orchestration.workers import WORKERS

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
            from app.agents.orchestration.escalation_service import EscalationService

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
    from app.agents.orchestration.logical_plan import (
        commit_frontier_results,
        load_logical_plan,
        logical_plan_progress,
        materialize_frontier,
        save_logical_plan,
    )
    from app.agents.orchestration.review import get_reviewer
    from app.agents.orchestration.temporal.client import load_job_llm_config
    from app.agents.orchestration.temporal_policy import evaluate_logical_effect_temporal
    from app.agents.orchestration.workers import WORKERS

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
            from app.agents.orchestration.escalation_service import EscalationService

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
    from app.agents.orchestration.approval_service import ApprovalService

    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None:
        return {"expired": False, "reason": "job_not_found"}
    expired = await ApprovalService(store=store).expire_if_due(job)
    job = await store.get_job(job_id) or job
    return {"expired": expired, "status": job.status.value}


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
async def replan_logical_read_activity(payload: dict) -> dict:
    """对已提交失败前沿执行一次无重试的纯读替代计划 Activity。"""
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"allowed": False, "reason": "missing_job_id"}
    from app.agents.orchestration.logical_plan import load_logical_plan

    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None:
        return {"allowed": False, "reason": "job_not_found"}
    pointer = (job.routing or {}).get("logical_plan") or {}
    plan = await load_logical_plan(job.user_id, str(pointer.get("plan_id") or ""))
    if not isinstance(plan, dict):
        return {"allowed": False, "reason": "logical_plan_unavailable"}
    try:
        allowed, reason = await _try_replan_pure_read_tail(job, plan, pointer)
    except Exception as exc:  # noqa: BLE001
        allowed, reason = False, f"replan_error:{str(exc)[:120]}"
    if allowed:
        await store.save_job(job)
        return {"allowed": True, "reason": reason}
    job.status = JobStatus.FAILED
    job.error = f"纯读逻辑计划自动重规划未通过: {reason}"
    job.routing = {**(job.routing or {}), "automatic_replan_blocked": reason}
    job.updated_at = time.time()
    await store.save_job(job)
    return {"allowed": False, "reason": reason}


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
