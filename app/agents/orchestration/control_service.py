"""Control-plane operations for running orchestration jobs.

The facade keeps the public methods for API compatibility, while this service
owns the runtime-neutral control sequence and backend fallback policy.
"""

from __future__ import annotations

from loguru import logger

from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.execution_backend import (
    LegacyDagBackend,
    TemporalManifestBackend,
)
from app.agents.orchestration.job_finalizer import JobFinalizer
from app.agents.orchestration.models import Job
from app.repositories.job_repository import JobRepository


class JobControlService:
    """Pause, resume, cancel and approve jobs across both runtimes."""

    def __init__(
        self,
        *,
        repository: JobRepository,
        approval: ApprovalService,
        temporal_backend: TemporalManifestBackend,
        legacy_backend: LegacyDagBackend,
        finalizer: JobFinalizer,
    ) -> None:
        self._repository = repository
        self._approval = approval
        self._temporal = temporal_backend
        self._legacy = legacy_backend
        self._finalizer = finalizer

    async def cancel(self, job_id: str, keep_completed: bool = True) -> Job | None:
        """Cancel a job and release admission capacity exactly once."""
        try:
            from app.agents.mcp.manager import cancel_task

            await cancel_task(job_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("取消任务时通知 MCP 失败 {}: {}", job_id, exc)

        stored_job = await self._repository.get_job(job_id)
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
        temporal_result = await self._temporal.approve(result.job, node_id, approved)
        if temporal_result is None:
            await self._legacy.approve(result.job, node_id, approved)

    async def pause(self, job_id: str) -> Job | None:
        stored_job = await self._repository.get_job(job_id)
        temporal_result = await self._temporal.pause(stored_job)
        if temporal_result is not None and temporal_result.handled:
            return temporal_result.job
        legacy_result = await self._legacy.pause(await self._repository.get_job(job_id))
        return legacy_result.job if legacy_result is not None else None

    async def resume(self, job_id: str) -> Job | None:
        stored_job = await self._repository.get_job(job_id)
        temporal_result = await self._temporal.resume(stored_job)
        if temporal_result is not None and temporal_result.handled:
            return temporal_result.job
        legacy_result = await self._legacy.resume(await self._repository.get_job(job_id))
        return legacy_result.job if legacy_result is not None else None
