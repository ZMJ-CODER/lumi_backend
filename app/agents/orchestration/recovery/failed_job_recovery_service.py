"""保留策略语义的普通办公失败任务恢复协调器。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from app.agents.orchestration.recovery.failed_job_replan_service import FailedJobReplanService
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.recovery.replan_policy import decide_failed_job_replan
from app.agents.orchestration.planning.tca import ComplexityLevel
from app.repositories.job_repository import JobRepository


class FailedJobRecoveryService:
    """Route a failure through deterministic controls before any LLM recovery."""

    def __init__(
        self,
        *,
        store: JobRepository,
        failed_replan: FailedJobReplanService,
        replan_logical_plan: Callable[[Job, str | None], Awaitable[bool]],
        handle_escalation: Callable[[Job], Awaitable[bool]],
        terminal_model_failure: Callable[[Job], bool],
        context_getter: Callable[[str], dict | None],
        planner_level_aware: Callable[[], bool],
        dynamic_enabled: Callable[[], bool],
        max_replans: Callable[[], int],
    ) -> None:
        self._store = store
        self._failed_replan = failed_replan
        self._replan_logical_plan = replan_logical_plan
        self._handle_escalation = handle_escalation
        self._terminal_model_failure = terminal_model_failure
        self._context_getter = context_getter
        self._planner_level_aware = planner_level_aware
        self._dynamic_enabled = dynamic_enabled
        self._max_replans = max_replans

    async def maybe_recover(self, job: Job, llm_api_key: str | None) -> bool:
        """Apply the stable recovery policy and mount one replacement if allowed."""
        if isinstance(job.routing, dict) and isinstance(job.routing.get("logical_plan"), dict):
            return await self._replan_logical_plan(job, llm_api_key)
        if self._terminal_model_failure(job):
            await self._store.save_job(job)
            return False
        if job.scene != "office" or job.status in {
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
        }:
            return False
        if await self._handle_escalation(job):
            return True

        # 阶段 3（EFFECT_JOURNAL_RECOVERY_V2，默认关闭）：自动恢复前先按 Effect Journal
        # 判定每个未完成步骤该跳过/重排/等人工。关闭时整段不执行，行为与改造前一致。
        effect_block = await self._apply_effect_journal_gate(job)
        if effect_block:
            return False

        from app.agents.orchestration.execution.safety import is_effectful
        from app.agents.orchestration.runtime.validation import validate_job_outcome

        outcome = validate_job_outcome(job)
        job.routing = dict(job.routing or {})
        validation_audit = outcome.model_dump(mode="json")
        try:
            from app.platform.security.agent_security import redact_server_text

            validation_audit["reason"] = redact_server_text(
                str(validation_audit.get("reason") or "")
            )
        except Exception:  # noqa: BLE001
            validation_audit["reason"] = str(validation_audit.get("reason") or "")[:500]
        job.routing["last_validation"] = validation_audit
        if outcome.valid:
            await self._store.save_job(job)
            return False
        if not outcome.may_upgrade:
            if job.status != JobStatus.FAILED:
                job.status = JobStatus.FAILED
            job.error = outcome.reason or "任务未生成可交付产物"
            job.updated_at = time.time()
            job.routing["automatic_replan_blocked"] = "non_replanable_validation_failure"
            await self._store.save_job(job)
            return False
        if any(is_effectful(node) and node.agent != "office_script" for node in job.nodes):
            job.routing["automatic_replan_blocked"] = "effectful_task"
            await self._store.save_job(job)
            return False

        current = ComplexityLevel(job.routing.get("level", "m2"))
        upgrade_count = int(job.routing.get("upgrade_count") or 0)
        replan_count = int(job.routing.get("replan_count") or 0)
        decision = decide_failed_job_replan(
            outcome,
            current=current,
            upgrade_count=upgrade_count,
            replan_count=replan_count,
            max_replans=self._max_replans(),
            dynamic_enabled=self._dynamic_enabled(),
            effectful=False,
        )
        if not decision.allowed:
            if decision.blocked_code:
                job.routing["automatic_replan_blocked"] = decision.blocked_code
            await self._store.save_job(job)
            return False
        if decision.target is None:
            await self._store.save_job(job)
            return False

        context = self._context_getter(job.job_id)
        if not context:
            job.routing["automatic_replan_blocked"] = "context_unavailable"
            await self._store.save_job(job)
            return False
        if not self._planner_level_aware():
            job.routing["automatic_replan_blocked"] = "planner_not_level_aware"
            await self._store.save_job(job)
            return False
        return await self._failed_replan.replan(
            job,
            target=decision.target,
            current=current,
            upgrade_count=upgrade_count,
            replan_count=replan_count,
            outcome_category=outcome.category.value,
            context=context,
            llm_api_key=llm_api_key,
        )

    async def _apply_effect_journal_gate(self, job: Job) -> bool:
        """按 Effect Journal 收敛自动恢复；返回 ``True`` 表示"已处理，不要再自动重排"。

        规则（方案 §5.4 + 阶段 3 红绿灯）：

        * ``confirmed`` 的未完成步骤 → 标记为已提交并跳过，不重放；
        * ``uncertain`` / ``intent`` / 日志不可用 / 高风险 / 无幂等键的写步骤 → 暂停等人工；
        * 只有只读步骤与带幂等键的幂等步骤才允许继续自动恢复。

        关闭开关时是空操作（不读日志、不写 routing、不改状态）。
        """
        from app.agents.orchestration.recovery import effect_journal_recovery as journal_recovery

        if not journal_recovery.is_enabled():
            return False
        outcome = await journal_recovery.plan_job_recovery(job)
        job.routing = dict(job.routing or {})
        job.routing["effect_recovery_plan"] = outcome.as_dict()
        if outcome.requires_human:
            # 人工确认前不得继续自动重排（整体红灯，避免"部分暂停、整体继续"）。
            job.routing["automatic_replan_blocked"] = "effect_journal_requires_human"
            job.status = JobStatus.PAUSED
            job.error = "存在副作用状态不确定或不可安全重放的步骤，已暂停等待人工确认。"
            job.updated_at = time.time()
            await self._store.save_job(job)
            return True
        if outcome.skip_step_ids:
            self._mark_confirmed_steps_skipped(job, outcome.skip_step_ids)
            await self._store.save_job(job)
        return False

    @staticmethod
    def _mark_confirmed_steps_skipped(job: Job, step_ids: tuple[str, ...]) -> None:
        """把"副作用已确认执行"的未完成步骤标记完成，避免重排时重复执行。"""
        wanted = {str(step_id) for step_id in step_ids}
        for node in job.nodes or []:
            if str(node.id) not in wanted:
                continue
            node.status = TaskStatus.COMPLETED
            node.error = None
            node.error_code = None
            node.effect_status = "committed"
            metadata = dict(node.metadata or {})
            metadata["recovery"] = "effect_already_committed"
            node.metadata = metadata
        job.updated_at = time.time()
