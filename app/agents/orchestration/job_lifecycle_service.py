"""办公任务提交后的生命周期职责。"""

from __future__ import annotations

from loguru import logger

from app.agents.orchestration.models import Job


class JobLifecycleService:
    """Keep terminal cleanup, plan learning, metrics, and display progress together."""

    def __init__(
        self,
        *,
        plan_contexts: dict[str, dict],
        llm_configs: dict[str, dict],
    ) -> None:
        self._plan_contexts = plan_contexts
        self._llm_configs = llm_configs

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

    async def finalize_plan(self, job: Job) -> None:
        """Leave a terminal hook without caching an executable LLM DAG.

        A workflow plan captures current authorization and capability scope;
        replaying it from a text-pattern cache is unsafe.  Future learning can
        use de-identified outcomes, not executable plans.
        """
        del job

    def cleanup_terminal(self, job: Job) -> None:
        """Release process-local context once a finalizer observes a terminal job."""
        self._plan_contexts.pop(job.job_id, None)
        self._llm_configs.pop(job.job_id, None)

    def discard_pending_learning(self, job_id: str) -> None:
        """Remove submission-local state when a job never reaches normal finalization."""
        self._plan_contexts.pop(job_id, None)

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
