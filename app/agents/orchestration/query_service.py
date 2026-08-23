"""Read-side orchestration service.

The write/execution coordinator should not also own Temporal query fallback,
task indexing and progress hydration.  This service centralizes those read and
terminal-cleanup rules while accepting callbacks for coordinator-owned state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger

from app.agents.orchestration.job_finalizer import JobFinalizer
from app.agents.orchestration.models import Job, JobStatus
from app.repositories.job_repository import JobRepository


class JobQueryService:
    def __init__(
        self,
        *,
        store: JobRepository,
        live_jobs: dict[str, Job],
        probe_temporal: Callable[[], Awaitable[bool]],
        stop_heartbeat: Callable[[str], Awaitable[None]],
        on_summary: Callable[[Job], Awaitable[None]],
        on_task_index: Callable[[Job], Awaitable[None]],
        on_metric: Callable[[Job], Awaitable[None]],
        on_learning: Callable[[Job], Awaitable[None]],
        attach_progress: Callable[[Job], Awaitable[Job]],
        on_terminal: Callable[[Job], None] | None = None,
        finalizer: JobFinalizer | None = None,
    ):
        self._store = store
        self._live_jobs = live_jobs
        self._probe_temporal = probe_temporal
        self._attach_progress = attach_progress
        self._finalizer = finalizer or JobFinalizer(
            stop_heartbeat=stop_heartbeat,
            on_summary=on_summary,
            on_task_index=on_task_index,
            on_metric=on_metric,
            on_learning=on_learning,
            on_terminal=on_terminal,
        )

    async def get_job(self, job_id: str) -> Job | None:
        job = await self._store.get_job(job_id)
        if (
            job is not None
            and str((job.routing or {}).get("runtime") or "") == "temporal_static"
            and await self._probe_temporal()
        ):
            try:
                from app.agents.orchestration.temporal.client import query_agent_job

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
                    if job.status in {
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                        JobStatus.INTERRUPTED,
                    }:
                        try:
                            await self._store.save_job(job)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("同步 Temporal 终态到 Redis 失败 {}: {}", job_id, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询 Temporal 任务状态失败，回退快照 {}: {}", job_id, exc)
        if job is None:
            live_job = self._live_jobs.get(job_id)
            if live_job is not None and live_job.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.PAUSED,
                JobStatus.WAITING_APPROVAL,
                JobStatus.WAITING_RESOURCES,
            }:
                logger.warning("Redis 未读到运行中任务快照，使用本地执行镜像: {}", job_id[:8])
                job = live_job.model_copy(deep=True)
        if job is None:
            return None

        if job.status == JobStatus.WAITING_APPROVAL:
            from app.agents.orchestration.approval_service import ApprovalService

            await ApprovalService(store=self._store).expire_if_due(job)

        await self._finalizer.finalize(job)
        return await self._attach_progress(job)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        jobs: list[Job] = []
        for job_id in await self._store.list_job_ids(user_id, limit):
            job = await self.get_job(job_id)
            if job:
                jobs.append(job)
        return jobs

    async def admin_list_jobs(self, limit: int = 50) -> list[Job]:
        jobs: list[Job] = []
        for job_id in await self._store.list_all_job_ids(limit):
            job = await self.get_job(job_id)
            if job:
                jobs.append(job)
        return jobs
