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
            from app.core.observability import inc_agent_job, mark_job_state

            status = job.status.value if hasattr(job.status, "value") else job.status
            # 标记里写**状态字符串**（不只是 "1"）：/metrics 的在途任务 Gauge 靠它区分
            # "completed" 与"还在跑"，否则只能知道"这个任务计过数"，无法算并发。
            # 恒覆盖写（不是 NX）：状态会从 running 变成 completed，首写留旧值会让
            # Gauge 永远把已完成的任务算成在途。
            await mark_job_state(job.job_id, str(status))
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
