"""所有执行运行时的 Job 错误统一收敛。"""

from __future__ import annotations

import time

from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import StateStore
from app.agents.orchestration.state_machine.errors import classify_error
from app.agents.orchestration.state_machine.transitions import transition


class JobErrorService:
    """Persist failure/interrupt outcomes without embedding policy in runners."""

    def __init__(self, *, store: StateStore) -> None:
        self._store = store

    async def fail(
        self,
        job_id: str,
        error: BaseException | str,
        *,
        error_code: str | None = None,
        result: dict | None = None,
    ) -> Job | None:
        """Load a job, classify its error, and converge it to ``FAILED``."""
        job = await self._store.get_job(job_id)
        if job is None:
            return None
        info = classify_error(error if isinstance(error, BaseException) else RuntimeError(error))
        if job.status not in {
            JobStatus.COMPLETED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        } and job.status != JobStatus.FAILED:
            transition(job, JobStatus.FAILED)
        if job.status == JobStatus.FAILED:
            job.error = str(error)[:1000]
            job.updated_at = time.time()
            if error_code or info.code:
                job.routing = {**(job.routing or {}), "error_code": error_code or info.code}
            if result is not None:
                job.result = result
            await self._store.save_job(job)
        return job

    async def interrupt(self, job_id: str, message: str = "任务被中断") -> Job | None:
        """Converge a still-running job to ``INTERRUPTED``."""
        job = await self._store.get_job(job_id)
        if job is None:
            return None
        if job.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            transition(job, JobStatus.INTERRUPTED)
            job.error = message[:1000]
            job.updated_at = time.time()
            await self._store.save_job(job)
        return job

    async def ensure_failed(self, job: Job, message: str) -> Job:
        """Make an unexpectedly non-terminal snapshot visible as failed."""
        if job.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.WAITING_APPROVAL,
            JobStatus.WAITING_RESOURCES,
        }:
            transition(job, JobStatus.FAILED)
            job.error = job.error or message[:1000]
            job.updated_at = time.time()
            await self._store.save_job(job)
        return job
