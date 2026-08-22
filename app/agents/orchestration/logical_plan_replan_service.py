"""Safe L3 replacement for failed rolling logical-plan frontiers.

The service owns the durable logical-plan mutation after the orchestrator has
already rejected terminal model failures and handled L2 approval signals.
Keeping it separate prevents the facade from duplicating planner, compiler,
and persistence mechanics for every rolling-plan recovery.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.plan_compilation_service import PlanCompilationService
from app.agents.orchestration.plan_context import PlanRequestContext
from app.agents.orchestration.replan_evidence_service import ReplanEvidenceService
from app.agents.orchestration.replan_policy import decide_logical_plan_replan
from app.agents.orchestration.tca import ComplexityLevel
from app.repositories.job_repository import JobRepository


class LogicalPlanReplanService:
    """Replace one unfinished logical-plan suffix when deterministic policy allows."""

    def __init__(
        self,
        *,
        store: JobRepository,
        workers: dict,
        plan_for_level: Callable[..., Awaitable[Any]],
        plan_compilation: PlanCompilationService,
        evidence: ReplanEvidenceService,
    ) -> None:
        self._store = store
        self._workers = workers
        self._plan_for_level = plan_for_level
        self._plan_compilation = plan_compilation
        self._evidence = evidence

    async def replan(
        self,
        job: Job,
        *,
        context: dict | None,
        llm_api_key: str | None,
        dynamic_enabled: bool,
        max_replans: int,
        planner_level_aware: bool,
    ) -> bool:
        """Plan, validate, and persist a replacement tail for a failed frontier."""
        pointer = (job.routing or {}).get("logical_plan")
        if not isinstance(pointer, dict) or not pointer.get("plan_id"):
            return False

        from app.agents.orchestration.logical_plan import (
            load_logical_plan,
            logical_plan_progress,
            materialize_frontier,
            replace_unfinished_tail,
            save_logical_plan,
        )

        plan = await load_logical_plan(job.user_id, str(pointer["plan_id"]))
        if not plan:
            job.status = JobStatus.FAILED
            job.error = "逻辑计划状态不可用，无法安全恢复失败步骤。"
            await self._store.save_job(job)
            return False

        failed_nodes = [
            node
            for node in job.nodes
            if node.status in {TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.SKIPPED}
        ]
        if not failed_nodes:
            return False

        records = plan.get("nodes") or {}
        committed_effect = any(
            str(record.get("status") or "") == TaskStatus.COMPLETED.value
            and str(record.get("effect_status") or "") in {"committed", "uncertain"}
            for record in records.values()
            if isinstance(record, dict)
        )
        current_effect = any(node.effect_status in {"committed", "uncertain"} for node in job.nodes)
        replan_count = int((job.routing or {}).get("replan_count") or 0)
        decision = decide_logical_plan_replan(
            dynamic_enabled=dynamic_enabled,
            replan_count=replan_count,
            max_replans=max_replans,
            effectful=committed_effect or current_effect,
        )
        if not decision.allowed:
            if decision.blocked_code:
                job.routing = {
                    **(job.routing or {}),
                    "automatic_replan_blocked": decision.blocked_code,
                }
            await self._store.save_job(job)
            return False

        if not context:
            job.routing = {
                **(job.routing or {}),
                "automatic_replan_blocked": "context_unavailable",
            }
            await self._store.save_job(job)
            return False
        if not planner_level_aware:
            job.routing = {
                **(job.routing or {}),
                "automatic_replan_blocked": "planner_not_level_aware",
            }
            await self._store.save_job(job)
            return False

        failed_evidence, evolution_context = await self._evidence.logical_plan_context(
            user_id=job.user_id,
            plan=plan,
            prior_summaries=str(context.get("prior_summaries") or ""),
        )
        tree = await self._plan_for_level(
            ComplexityLevel.M3,
            PlanRequestContext.from_mapping(context)
            .with_llm_api_key(llm_api_key)
            .with_prior_summaries(evolution_context),
            bypass_fast_paths=True,
        )
        if tree.error or not tree.nodes:
            job.routing = {
                **(job.routing or {}),
                "replan_error": tree.error or tree.clarification or "未生成可执行替代步骤",
            }
            await self._store.save_job(job)
            return False

        self._plan_compilation.normalize_for_replan(
            tree.nodes,
            job.request,
            preserve_dependencies=False,
            adapt_workers=True,
        )
        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.plan_compiler import CompileDecision, compile_plan
        from app.agents.orchestration.presentation import attach_display_plan
        from app.agents.orchestration.safety import prepare_node_safety

        next_revision = int(plan.get("revision") or 1) + 1
        for node in tree.nodes:
            node.metadata = {**(node.metadata or {}), "plan_revision": next_revision}
            attach_display_plan(node)
            prepare_node_safety(node, job.user_id, job.job_id)
        compiled = await compile_plan(
            tree.nodes,
            scene="office",
            user_role=job.user_role,
            user_id=job.user_id,
            workers=self._workers,
        )
        job.routing = dict(job.routing or {})
        job.routing["plan_compiler"] = {
            "decision": compiled.decision.value,
            "capability_fingerprint": compiled.capabilities.fingerprint,
            "cost": compiled.cost.model_dump(mode="json"),
            "violations": [item.model_dump(mode="json") for item in compiled.violations[:8]],
            "warnings": [item.model_dump(mode="json") for item in compiled.warnings[:8]],
        }
        if compiled.decision == CompileDecision.REPLAN_REQUIRED:
            job.routing["replan_error"] = "替代计划未通过执行前编译检查：" + "；".join(
                item.message for item in compiled.violations[:5]
            )[:500]
            await self._store.save_job(job)
            return False
        tree.nodes = compiled.nodes
        errors = validate_planned_dag(tree.nodes, self._workers)
        if errors:
            job.routing["replan_error"] = "；".join(errors)[:500]
            await self._store.save_job(job)
            return False

        failed_names = [item["step"] for item in failed_evidence if item.get("step")]
        reason = (
            f"原计划中的“{'、'.join(failed_names[:2])}”未能完成，已根据执行结果更换方法。"
            if failed_names
            else "原计划未能完成，已根据执行结果更换方法。"
        )
        replace_unfinished_tail(plan, tree.nodes, reason=reason)
        frontier = materialize_frontier(plan)
        if not frontier:
            job.status = JobStatus.FAILED
            job.error = "替代计划没有可执行前沿，已停止以避免重复执行。"
            job.routing = {**(job.routing or {}), "replan_error": "replacement_frontier_empty"}
            await save_logical_plan(job.user_id, plan)
            await self._store.save_job(job)
            return False

        plan_history = list((job.routing or {}).get("plan_history") or [])
        plan_history.append(
            {
                "revision": int(pointer.get("revision") or 1),
                "reason": reason,
                "changed_at": time.time(),
            }
        )
        job.nodes = frontier
        job.plan_text = tree.plan_text
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.updated_at = time.time()
        job.routing = {
            **(job.routing or {}),
            "level": ComplexityLevel.M3.value,
            "mode": "react",
            "replan_count": replan_count + 1,
            "plan_revision": plan.get("revision"),
            "plan_history": plan_history[-3:],
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
        await self._store.save_job(job)
        try:
            from app.core.observability import inc_agent_replan

            inc_agent_replan("logical", ComplexityLevel.M3.value, "logical_plan_failure")
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "逻辑计划失败后挂载替代尾部: job={} revision={} ",
            job.job_id[:8],
            plan.get("revision"),
        )
        return True
