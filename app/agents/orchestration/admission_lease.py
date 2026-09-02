"""从编排门面分离的准入租约心跳。"""

from __future__ import annotations

import asyncio

from loguru import logger

from app.agents.orchestration.admission import job_admission
from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.job_error_service import JobErrorService
from app.agents.orchestration.state import StateStore
from app.agents.orchestration.models import JobStatus
from app.agents.orchestration.state_machine.policies import is_terminal
from app.core.config import settings


class AdmissionLeaseMonitor:
    """Renew active-job admission leases and stop jobs after lease loss."""

    def __init__(
        self,
        *,
        store: StateStore,
        tasks: dict[str, asyncio.Task],
        error_service: JobErrorService,
        interval_seconds: float | None = None,
    ) -> None:
        self._store = store
        self._tasks = tasks
        self._errors = error_service
        self._interval_seconds = interval_seconds
        self._heartbeats: dict[str, asyncio.Task] = {}

    def start(self, job_id: str, user_id: str) -> None:
        current = self._heartbeats.get(job_id)
        if current is not None and not current.done():
            return

        async def heartbeat() -> None:
            interval = self._interval_seconds
            if interval is None:
                interval = max(10.0, min(60.0, settings.AGENT_ADMISSION_LEASE_SECONDS / 3))
            try:
                while True:
                    await asyncio.sleep(interval)
                    job = await self._store.get_job(job_id)
                    if job is None or is_terminal(job.status):
                        await job_admission.release(job_id=job_id, user_id=user_id)
                        return
                    if job.status == JobStatus.WAITING_APPROVAL:
                        if await ApprovalService(store=self._store).expire_if_due(job):
                            await job_admission.release(job_id=job_id, user_id=user_id)
                            return
                    if not await job_admission.renew(job_id, user_id):
                        logger.error("办公任务准入租约丢失，停止任务: {}", job_id[:8])
                        job = await self._store.get_job(job_id)
                        if job and job.status in {
                            JobStatus.PENDING,
                            JobStatus.RUNNING,
                            JobStatus.PAUSED,
                            JobStatus.WAITING_APPROVAL,
                            JobStatus.WAITING_RESOURCES,
                            JobStatus.CONTINUING,
                        }:
                            await self._errors.interrupt(
                                job_id,
                                "任务运行租约已失效，为避免并发超限已自动停止。",
                            )
                            task = self._tasks.get(job_id)
                            if task is not None and not task.done():
                                task.cancel()
                        return
            except asyncio.CancelledError:
                return
            finally:
                current_task = asyncio.current_task()
                if self._heartbeats.get(job_id) is current_task:
                    self._heartbeats.pop(job_id, None)

        self._heartbeats[job_id] = asyncio.create_task(heartbeat())

    async def stop(self, job_id: str) -> None:
        task = self._heartbeats.pop(job_id, None)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
