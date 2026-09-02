"""显式授权滚动清单的 Temporal 后端。"""

from __future__ import annotations

import time

from app.agents.orchestration.backends.contracts import BackendControlResult
from app.agents.orchestration.job_contract import freeze_job_spec
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.state_machine.transitions import transition
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


class TemporalManifestBackend:
    """Submit an explicitly authorized rolling manifest to Temporal."""

    name = "manifest_temporal"

    def __init__(self, runtime: RuntimeGateway) -> None:
        self._runtime = runtime

    async def submit(
        self, job: Job, llm_api_key: str | None, llm_config: dict | None = None
    ) -> None:
        freeze_job_spec(job)
        await self._runtime.submit_manifest(job, llm_api_key, llm_config)

    async def _ready(self, job: Job | None) -> bool:
        return bool(job and RuntimeGateway.is_manifest_job(job) and await self._runtime.probe_temporal())

    async def cancel(
        self, job: Job | None, keep_completed: bool = True
    ) -> BackendControlResult | None:
        if not await self._ready(job):
            return None
        assert job is not None
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
            return BackendControlResult(job)
        try:
            await self._runtime.cancel_manifest(job.job_id, keep_completed)
            transition(job, JobStatus.CANCELLED)
            job.updated_at = time.time()
            await self._runtime.store.save_job(job)
            return BackendControlResult(job, release_capacity=True)
        except Exception as exc:  # noqa: BLE001
            self._fallback("取消", "TEMPORAL_CANCEL_FALLBACK", job, exc)
            return None

    async def pause(self, job: Job | None) -> BackendControlResult | None:
        if not await self._ready(job):
            return None
        assert job is not None
        try:
            if job.status == JobStatus.RUNNING:
                await self._runtime.pause_manifest(job.job_id)
                transition(job, JobStatus.PAUSED)
                job.updated_at = time.time()
                await self._runtime.store.save_job(job)
            return BackendControlResult(job)
        except Exception as exc:  # noqa: BLE001
            self._fallback("暂停", "TEMPORAL_PAUSE_FALLBACK", job, exc)
            return None

    async def resume(self, job: Job | None) -> BackendControlResult | None:
        if not await self._ready(job):
            return None
        assert job is not None
        try:
            if job.status == JobStatus.PAUSED:
                await self._runtime.resume_manifest(job.job_id)
                transition(job, JobStatus.RUNNING)
                job.updated_at = time.time()
                await self._runtime.store.save_job(job)
            return BackendControlResult(job)
        except Exception as exc:  # noqa: BLE001
            self._fallback("恢复", "TEMPORAL_RESUME_FALLBACK", job, exc)
            return None

    async def approve(
        self, job: Job, node_id: str, approved: bool
    ) -> BackendControlResult | None:
        """Manifest items are authorized before the workflow is started."""
        return None

    def _fallback(self, action: str, code: str, job: Job, exc: Exception) -> None:
        monitor_logger.warning(
            f"Temporal 清单{action}控制失败，回退 legacy",
            event_type="runtime_control_fallback",
            category="external_service",
            code=code,
            context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
            metadata={"error": str(exc)},
        )
