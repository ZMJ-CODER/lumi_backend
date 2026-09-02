"""办公任务提交后的生命周期职责。"""

from __future__ import annotations

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.plan_cache import PlanCache


class JobLifecycleService:
    """Keep terminal cleanup, plan learning, metrics, and display progress together."""

    def __init__(
        self,
        *,
        plan_cache: PlanCache,
        plan_contexts: dict[str, dict],
        llm_configs: dict[str, dict],
        pending_plan_cache: dict[str, tuple[str, list[dict] | None]],
    ) -> None:
        self._plan_cache = plan_cache
        self._plan_contexts = plan_contexts
        self._llm_configs = llm_configs
        self._pending_plan_cache = pending_plan_cache

    async def record_metric(self, job: Job) -> None:
        """Count each terminal job once without affecting execution on telemetry failures."""
        try:
            from app.core.observability import inc_agent_job
            from app.core.redis import get_redis

            key = f"obs:job:{job.job_id}"
            if await get_redis().set(key, "1", ex=86400 * 7, nx=True):
                status = job.status.value if hasattr(job.status, "value") else job.status
                inc_agent_job(str(status))
        except Exception:  # noqa: BLE001
            pass

    async def learn_from_finished_job(self, job: Job) -> None:
        """Commit only successful plans to the reuse cache."""
        pending = self._pending_plan_cache.get(job.job_id)
        if job.status != JobStatus.COMPLETED or not pending:
            if job.status in {
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.INTERRUPTED,
            }:
                self._pending_plan_cache.pop(job.job_id, None)
            return
        key, office_docs = pending
        if await self._plan_cache.put(key, job.nodes, office_docs, job.plan_text):
            self._pending_plan_cache.pop(job.job_id, None)
            try:
                from app.core.observability import inc_plan_cache

                inc_plan_cache("stored")
            except Exception:  # noqa: BLE001
                pass

    def cleanup_terminal(self, job: Job) -> None:
        """Release process-local context once a finalizer observes a terminal job."""
        self._plan_contexts.pop(job.job_id, None)
        self._llm_configs.pop(job.job_id, None)

    def discard_pending_learning(self, job_id: str) -> None:
        """Remove submission-local state when a job never reaches normal finalization."""
        self._plan_contexts.pop(job_id, None)
        self._pending_plan_cache.pop(job_id, None)

    async def attach_progress(self, job: Job) -> Job:
        """Merge transient node progress into a response-only job snapshot."""
        try:
            from app.agents.core.progress import get_job_progress

            progress = await get_job_progress(job.job_id)
            if progress:
                for node in job.nodes:
                    text = progress.get(node.id)
                    if text:
                        node.metadata = {**(node.metadata or {}), "progress": str(text)}
        except Exception as exc:  # noqa: BLE001
            logger.debug("合并任务进度失败 {}: {}", job.job_id, exc)
        return job
