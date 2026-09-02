"""运行中编排任务的控制面操作。

The facade keeps the public methods for API compatibility, while this service
owns the runtime-neutral control sequence and backend fallback policy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger

from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.backends.legacy import LegacyDagBackend
from app.agents.orchestration.backends.temporal_logical_effects import TemporalLogicalEffectsBackend
from app.agents.orchestration.backends.temporal_logical_read import TemporalLogicalReadBackend
from app.agents.orchestration.backends.temporal_manifest import TemporalManifestBackend
from app.agents.orchestration.backends.temporal_static import TemporalStaticBackend
from app.agents.orchestration.job_finalizer import JobFinalizer
from app.agents.orchestration.models import Job, JobStatus
from app.repositories.job_repository import JobRepository


class _UnavailableBackend:
    """Compatibility placeholder for deployments without an optional runtime."""

    async def cancel(self, *args, **kwargs):
        return None

    async def pause(self, *args, **kwargs):
        return None

    async def resume(self, *args, **kwargs):
        return None

    async def approve(self, *args, **kwargs):
        return None


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
        logical_read_backend: TemporalLogicalReadBackend | None = None,
        logical_effects_backend: TemporalLogicalEffectsBackend | None = None,
        ensure_active_capacity: Callable[[Job], Awaitable[bool]] | None = None,
    ) -> None:
        self._repository = repository
        self._approval = approval
        self._temporal = temporal_backend
        self._static = static_backend
        self._logical_read = logical_read_backend or _UnavailableBackend()
        self._logical_effects = logical_effects_backend or _UnavailableBackend()
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
        effects_result = await self._logical_effects.cancel(stored_job, keep_completed)
        if effects_result is not None and effects_result.handled:
            if effects_result.error:
                raise RuntimeError(effects_result.error)
            await self._release_cancelled_capacity(effects_result.job, effects_result.release_capacity)
            return effects_result.job
        logical_result = await self._logical_read.cancel(stored_job, keep_completed)
        if logical_result is not None and logical_result.handled:
            if logical_result.error:
                raise RuntimeError(logical_result.error)
            await self._release_cancelled_capacity(logical_result.job, logical_result.release_capacity)
            return logical_result.job
        static_result = await self._static.cancel(stored_job, keep_completed)
        if static_result is not None and static_result.handled:
            if static_result.error:
                raise RuntimeError(static_result.error)
            await self._release_cancelled_capacity(static_result.job, static_result.release_capacity)
            return static_result.job
        temporal_result = await self._temporal.cancel(stored_job, keep_completed)
        if temporal_result is not None and temporal_result.handled:
            await self._release_cancelled_capacity(temporal_result.job, temporal_result.release_capacity)
            return temporal_result.job

        legacy_result = await self._legacy.cancel(
            await self._repository.get_job(job_id), keep_completed
        )
        if legacy_result is None:
            return None
        await self._release_cancelled_capacity(legacy_result.job, legacy_result.release_capacity)
        return legacy_result.job

    async def _release_cancelled_capacity(self, job: Job | None, release_capacity: bool) -> None:
        if release_capacity:
            await self._finalizer.finalize(job)
        elif job is not None and job.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            # The runner may reach a terminal state just before this cancel
            # request. Its normal finalizer can then lose the race with the
            # backend's no-op cancellation response, leaving a live admission
            # lease. Releasing and stopping the heartbeat are idempotent.
            await self._finalizer.suspend_capacity(job)

    async def approve(self, job_id: str, node_id: str, approved: bool = True) -> None:
        """Resolve the persisted approval gate, then resume the active backend."""
        # Temporal owns the live waiting snapshot.  Hydrate it before the
        # repository-backed ApprovalService validates the gate, otherwise a
        # freshly paused workflow would still look RUNNING in Redis.
        stored = await self._repository.get_job(job_id)
        if stored is not None and str((stored.routing or {}).get("runtime") or "") == "temporal_static":
            try:
                from app.agents.orchestration.temporal.client import query_agent_job

                snap = await query_agent_job(job_id)
                if snap is not None:
                    from app.agents.orchestration.models import Job as JobModel

                    hydrated = JobModel.model_validate(snap)
                    await self._repository.save_job(hydrated)
            except Exception as exc:  # noqa: BLE001
                logger.debug("审批前同步 Temporal 快照失败 {}: {}", job_id, exc)
        result = await self._approval.resolve(job_id, node_id, approved)
        if approved and self._ensure_active_capacity is not None and not await self._ensure_active_capacity(result.job):
            # Approval is durable and bound to the exact fingerprint, but a
            # newly active task still needs ordinary admission. Do not retain
            # a hidden active slot while the user waits for capacity.
            result.job.status = JobStatus.PAUSED
            result.job.error = "审批已通过，但当前执行容量已满；请稍后恢复任务。"
            await self._repository.save_job(result.job)
            raise RuntimeError("任务已获批准，但当前执行容量已满；请稍后重新恢复任务。")
        static_result = await self._static.approve(result.job, node_id, approved)
        if static_result is not None and static_result.handled:
            if static_result.error:
                raise RuntimeError(static_result.error)
            return
        effects_result = await self._logical_effects.approve(result.job, node_id, approved)
        if effects_result is not None and effects_result.handled:
            if effects_result.error:
                raise RuntimeError(effects_result.error)
            return
        temporal_result = await self._temporal.approve(result.job, node_id, approved)
        if temporal_result is None:
            await self._legacy.approve(result.job, node_id, approved)

    async def pause(self, job_id: str) -> Job | None:
        stored_job = await self._repository.get_job(job_id)
        effects_result = await self._logical_effects.pause(stored_job)
        if effects_result is not None and effects_result.handled:
            if effects_result.error:
                raise RuntimeError(effects_result.error)
            return effects_result.job
        logical_result = await self._logical_read.pause(stored_job)
        if logical_result is not None and logical_result.handled:
            if logical_result.error:
                raise RuntimeError(logical_result.error)
            return logical_result.job
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
        effects_result = await self._logical_effects.resume(stored_job)
        if effects_result is not None and effects_result.handled:
            if effects_result.error:
                raise RuntimeError(effects_result.error)
            return effects_result.job
        logical_result = await self._logical_read.resume(stored_job)
        if logical_result is not None and logical_result.handled:
            if logical_result.error:
                raise RuntimeError(logical_result.error)
            return logical_result.job
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
