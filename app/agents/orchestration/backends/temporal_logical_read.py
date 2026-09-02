"""严格纯读逻辑计划的 Temporal 后端。"""

from __future__ import annotations

import time

from app.agents.orchestration.backends.contracts import BackendControlResult
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.state_machine.transitions import transition
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


class TemporalLogicalReadBackend:
    """Only accepts the allowlisted pure-read logical-plan subset."""

    name = "temporal_logical_read"

    def __init__(self, runtime: RuntimeGateway) -> None:
        self._runtime = runtime

    async def submit(
        self, job: Job, llm_api_key: str | None, llm_config: dict | None = None
    ) -> None:
        await self._runtime.submit_logical_read(job, llm_api_key, llm_config)

    @staticmethod
    def _owns(job: Job | None) -> bool:
        return RuntimeGateway.is_logical_read_job(job)

    async def _control(
        self, job: Job | None, signal: str, keep_completed: bool = True
    ) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        assert job is not None
        if not await self._runtime.probe_temporal():
            return BackendControlResult(job, error="Temporal Worker 当前不可达，控制请求未送达，请检查连接后重试。")
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
            return BackendControlResult(job)
        try:
            from app.agents.orchestration.temporal.client import signal_logical_read_workflow

            await signal_logical_read_workflow(
                job.job_id, signal, keep_completed if signal == "cancel_request" else None
            )
            release_capacity = self._apply_state(job, signal)
            job.updated_at = time.time()
            await self._runtime.store.save_job(job)
            return BackendControlResult(job, release_capacity=release_capacity)
        except Exception as exc:  # noqa: BLE001
            monitor_logger.warning(
                "Temporal 纯读逻辑计划控制失败",
                event_type="runtime_control_failure",
                category="external_service",
                code="TEMPORAL_LOGICAL_READ_CONTROL_FAILED",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
            return BackendControlResult(job, error="Temporal Worker 控制请求失败，任务状态未改变，请检查连接后重试。")

    @staticmethod
    def _apply_state(job: Job, signal: str) -> bool:
        if signal == "cancel_request":
            transition(job, JobStatus.CANCELLED)
            return True
        if signal == "pause" and job.status == JobStatus.RUNNING:
            transition(job, JobStatus.PAUSED)
        elif signal == "resume" and job.status == JobStatus.PAUSED:
            transition(job, JobStatus.RUNNING)
        return False

    async def cancel(self, job: Job | None, keep_completed: bool = True) -> BackendControlResult | None:
        return await self._control(job, "cancel_request", keep_completed)

    async def pause(self, job: Job | None) -> BackendControlResult | None:
        return await self._control(job, "pause")

    async def resume(self, job: Job | None) -> BackendControlResult | None:
        return await self._control(job, "resume")

    async def approve(self, job: Job, node_id: str, approved: bool) -> BackendControlResult | None:
        if self._owns(job):
            return BackendControlResult(job, error="纯读逻辑计划不支持审批节点，请以 Legacy 任务重新提交。")
        return None
