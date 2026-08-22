"""Policy-preserving recovery coordinator for ordinary failed office jobs."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from app.agents.orchestration.failed_job_replan_service import FailedJobReplanService
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.replan_policy import decide_failed_job_replan
from app.agents.orchestration.tca import ComplexityLevel
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
        if isinstance(job.routing, dict) and isinstance(job.routing.get("manifest"), dict):
            return False
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

        from app.agents.orchestration.safety import is_effectful
        from app.agents.orchestration.validation import validate_job_outcome

        outcome = validate_job_outcome(job)
        job.routing = dict(job.routing or {})
        validation_audit = outcome.model_dump(mode="json")
        try:
            from app.core.agent_security import redact_server_text

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
