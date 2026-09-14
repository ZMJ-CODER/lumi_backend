"""恢复与重规划的**协调器**。

这些方法只做"把出错的任务导到正确的恢复路径"：终态模型失败判定、逻辑计划续跑与
重规划、失败任务恢复、升级（澄清/审批）处理、单步失败的终态收尾。
它们**不是**正常执行流程的一部分——集中在一处，熔断、幂等与审计才有单一落点
（见 :mod:`app.agents.orchestration.recovery` 的其余模块）。

``AgentOrchestrator`` 通过混入本类拿到这些能力，对外接口不变。
"""

from __future__ import annotations


from loguru import logger

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.core.config import settings


class RecoveryCoordinationMixin:
    """恢复/重规划协调（混入 ``AgentOrchestrator``；不定义 ``__init__``）。"""

    async def _continue_logical_plan(self, job: Job) -> bool:
        """Commit a single ordinary-DAG frontier and materialize the next one."""
        return await self._logical_plan.continue_job(job)

    async def _maybe_replan_logical_plan(self, job: Job, llm_api_key: str | None) -> bool:
        """Apply approval safety controls, then delegate safe L3 recovery."""
        pointer = (job.routing or {}).get("logical_plan")
        if not isinstance(pointer, dict) or not pointer.get("plan_id"):
            return False
        if job.scene != "office" or job.status in {
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
        }:
            return False
        if self._has_terminal_model_failure(job):
            await self._store.save_job(job)
            return False

        # L2 stays in the prebuilt approval/clarification controller.  An
        # approved node remains the same logical node, so reopen only its
        # materialized record for the later retry instead of changing the
        # logical graph or consuming another L3 attempt.
        if await self._handle_task_escalation(job):
            if job.status == JobStatus.WAITING_APPROVAL:
                from app.agents.orchestration.planning.logical_plan import (
                    load_logical_plan,
                    logical_plan_progress,
                    save_logical_plan,
                )

                plan = await load_logical_plan(job.user_id, str(pointer["plan_id"]))
                if not plan:
                    job.status = JobStatus.FAILED
                    job.error = "逻辑计划状态不可用，无法安全恢复失败步骤。"
                    await self._store.save_job(job)
                    return False
                records = plan.get("nodes") or {}
                for node in job.nodes:
                    logical_id = str((node.metadata or {}).get("logical_node_id") or node.id)
                    record = records.get(logical_id)
                    if isinstance(record, dict) and node.status == TaskStatus.PENDING:
                        record["status"] = "materialized"
                        record["error"] = ""
                        record["error_code"] = ""
                job.routing = dict(job.routing or {})
                job.routing["logical_plan"] = {
                    **pointer,
                    "revision": plan.get("revision", 1),
                    "progress": logical_plan_progress(plan),
                }
                await save_logical_plan(job.user_id, plan)
                await self._store.save_job(job)
            return True

        context = self._job_plan_context.get(job.job_id)
        return await self._logical_plan_replan.replan(
            job,
            context=context,
            llm_api_key=llm_api_key,
            dynamic_enabled=settings.AGENT_DYNAMIC_SUBGRAPH_ENABLED,
            max_replans=settings.AGENT_SUBGRAPH_MAX_REPLANS,
            planner_level_aware=callable(getattr(self._planner, "plan_for_level", None)),
        )

    async def _maybe_replan_failed_job(self, job: Job, llm_api_key: str | None) -> bool:
        """Delegate ordinary failure recovery to the policy coordinator."""
        return await self._failed_job_recovery.maybe_recover(job, llm_api_key)

    async def _handle_task_escalation(self, job: Job) -> bool:
        """Resolve L2 signals through deterministic orchestration controls.

        Missing prerequisites become a stable clarification result. Confirmation
        signals make one existing node wait for the API approval flow.  Neither
        branch permits an arbitrary new edge/node supplied by a worker.
        """
        return await self._escalation.handle_task_escalation(job)

    async def _finalize_step_failed(self, job: Job) -> None:
        """单步执行失败：先按升级决策树记录建议，再做终态清理。"""
        try:
            from app.services.upgrade_adapter import suggest_upgrade

            result = job.result if isinstance(job.result, dict) else {}
            error_code = str(
                result.get("error_code")
                or next((node.error_code for node in job.nodes if node.error_code), "")
                or ""
            )
            current = str((job.routing or {}).get("complexity") or "M1")
            suggestion = suggest_upgrade(error_code, current=current)
            if suggestion:
                job.routing = {**(job.routing or {}), "upgrade": suggestion}
                await self._store.save_job(job)
        except Exception as exc:  # noqa: BLE001 - 建议记录失败不影响终态清理
            logger.warning("记录升级建议失败 {}: {}", str(job.job_id)[:12], str(exc)[:160])
        await self._finalizer.finalize(job)
