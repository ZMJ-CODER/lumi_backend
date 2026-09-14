"""_try_replan_pure_read_tail replan_logical_read_activity（logical_read_activities 的 replan 族）。"""

from __future__ import annotations
import time
from temporalio import activity
from app.agents.orchestration.models import JobStatus
from app.agents.orchestration.runtime.state import RedisStateStore
from app.core.config import settings


async def _try_replan_pure_read_tail(job, plan: dict, pointer: dict) -> tuple[bool, str]:
    """由 Activity 进行一次受限 LLM 重规划，成功后挂载新的纯读尾部。

    这是 Temporal Activity 而非 Workflow 的工作：其中会读取 Redis 上下文、调用
    Planner 并执行编译校验。调用方以单次 Activity 执行保障同一失败前沿不会被
    Temporal 重试多次规划。
    """
    from app.agents.orchestration.execution.validation import validate_planned_dag
    from app.agents.orchestration.planning.logical_plan import (
        logical_plan_progress,
        materialize_frontier,
        replace_unfinished_tail,
        save_logical_plan,
    )
    from app.agents.orchestration.planning.plan_compiler import CompileDecision, compile_plan
    from app.agents.orchestration.planning.context import PlanRequestContext
    from app.agents.orchestration.planning.planner import LlmPlanner
    from app.agents.orchestration.recovery.replan_evidence_service import ReplanEvidenceService
    from app.agents.orchestration.planning.tca import ComplexityLevel
    from app.agents.orchestration.temporal.client import (
        load_job_llm_config,
        load_temporal_replan_context,
    )
    from app.agents.orchestration.runtime.temporal_policy import evaluate_logical_read_temporal
    from app.agents.orchestration.execution.workers import WORKERS

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
        workspace_id=str(replan_context.get("workspace_id") or "") or None,
        workspace_summary=str(replan_context.get("workspace_summary") or ""),
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
        from app.agents.orchestration.execution.safety import prepare_node_safety

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
    from app.agents.orchestration.planning.logical_plan import logical_plan_execution_fingerprint

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


@activity.defn
async def replan_logical_read_activity(payload: dict) -> dict:
    """对已提交失败前沿执行一次无重试的纯读替代计划 Activity。"""
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"allowed": False, "reason": "missing_job_id"}
    from app.agents.orchestration.planning.logical_plan import load_logical_plan

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
