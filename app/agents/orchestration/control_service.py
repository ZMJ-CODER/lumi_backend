"""Control-plane operations for running orchestration jobs.

The facade keeps the public methods for API compatibility, while this service
owns the runtime-neutral control sequence and backend fallback policy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger

from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.execution_backend import (
    LegacyDagBackend,
    TemporalManifestBackend,
    TemporalStaticBackend,
)
from app.agents.orchestration.job_finalizer import JobFinalizer
from app.agents.orchestration.models import Job, JobStatus
from app.repositories.job_repository import JobRepository


class JobControlService:
    """Pause, resume, cancel and approve jobs across both runtimes."""

    def __init__(
        self,
        *,
        repository: JobRepository,
        approval: ApprovalService,
        temporal_backend: TemporalManifestBackend,
        static_backend: TemporalStaticBackend,
        legacy_backend: LegacyDagBackend,
        finalizer: JobFinalizer,
        ensure_active_capacity: Callable[[Job], Awaitable[bool]] | None = None,
    ) -> None:
        self._repository = repository
        self._approval = approval
        self._temporal = temporal_backend
        self._static = static_backend
        self._legacy = legacy_backend
        self._finalizer = finalizer
        self._ensure_active_capacity = ensure_active_capacity

    async def cancel(self, job_id: str, keep_completed: bool = True) -> Job | None:
        """Cancel a job and release admission capacity exactly once."""
        try:
            from app.agents.mcp.manager import cancel_task

            await cancel_task(job_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("取消任务时通知 MCP 失败 {}: {}", job_id, exc)

        stored_job = await self._repository.get_job(job_id)
        static_result = await self._static.cancel(stored_job, keep_completed)
        if static_result is not None and static_result.handled:
            if static_result.error:
                raise RuntimeError(static_result.error)
            if static_result.release_capacity:
                await self._finalizer.finalize(static_result.job)
            return static_result.job
        temporal_result = await self._temporal.cancel(stored_job, keep_completed)
        if temporal_result is not None and temporal_result.handled:
            if temporal_result.release_capacity:
                await self._finalizer.finalize(temporal_result.job)
            return temporal_result.job

        legacy_result = await self._legacy.cancel(
            await self._repository.get_job(job_id), keep_completed
        )
        if legacy_result is None:
            return None
        if legacy_result.release_capacity:
            await self._finalizer.finalize(legacy_result.job)
        return legacy_result.job

    async def approve(self, job_id: str, node_id: str, approved: bool = True) -> None:
        """Resolve the persisted approval gate, then resume the active backend."""
        result = await self._approval.resolve(job_id, node_id, approved)
        if not approved:
            return
        if self._ensure_active_capacity is not None and not await self._ensure_active_capacity(result.job):
            # Approval is durable and bound to the exact fingerprint, but a
            # newly active task still needs ordinary admission. Do not retain
            # a hidden active slot while the user waits for capacity.
            result.job.status = JobStatus.PAUSED
            result.job.error = "审批已通过，但当前执行容量已满；请稍后恢复任务。"
            await self._repository.save_job(result.job)
            raise RuntimeError("任务已获批准，但当前执行容量已满；请稍后重新恢复任务。")
        static_result = await self._static.approve(result.job, node_id, approved)
        if static_result is not None and static_result.handled:
            return
        temporal_result = await self._temporal.approve(result.job, node_id, approved)
        if temporal_result is None:
            await self._legacy.approve(result.job, node_id, approved)

    async def pause(self, job_id: str) -> Job | None:
        stored_job = await self._repository.get_job(job_id)
        static_result = await self._static.pause(stored_job)
        if static_result is not None and static_result.handled:
            if static_result.error:
                raise RuntimeError(static_result.error)
            return static_result.job
        temporal_result = await self._temporal.pause(stored_job)
        if temporal_result is not None and temporal_result.handled:
            return temporal_result.job
        legacy_result = await self._legacy.pause(await self._repository.get_job(job_id))
        return legacy_result.job if legacy_result is not None else None

    async def resume(self, job_id: str) -> Job | None:
        stored_job = await self._repository.get_job(job_id)
        if (
            stored_job is not None
            and stored_job.status.name == "PAUSED"
            and self._ensure_active_capacity is not None
        ):
            if not await self._ensure_active_capacity(stored_job):
                return stored_job
        static_result = await self._static.resume(stored_job)
        if static_result is not None and static_result.handled:
            if static_result.error:
                raise RuntimeError(static_result.error)
            return static_result.job
        temporal_result = await self._temporal.resume(stored_job)
        if temporal_result is not None and temporal_result.handled:
            return temporal_result.job
        legacy_result = await self._legacy.resume(await self._repository.get_job(job_id))
        return legacy_result.job if legacy_result is not None else None
