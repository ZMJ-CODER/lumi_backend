"""用于预声明、已审批逻辑计划副作用的 Temporal 后端。"""

from __future__ import annotations

import time
from typing import Any

from app.agents.orchestration.backends.contracts import BackendControlResult
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.state_machine.transitions import transition
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


class TemporalLogicalEffectsBackend:
    """Run predeclared, approval-gated effects through Temporal."""

    name = "temporal_logical_effects"

    def __init__(self, runtime: RuntimeGateway) -> None:
        self._runtime = runtime

    async def submit(
        self, job: Job, llm_api_key: str | None, llm_config: dict | None = None
    ) -> None:
        await self._runtime.submit_logical_effects(job, llm_api_key, llm_config)

    @staticmethod
    def _owns(job: Job | None) -> bool:
        return RuntimeGateway.is_logical_effects_job(job)

    async def _signal(
        self, job: Job | None, signal: str, arg: Any = None
    ) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        assert job is not None
        if not await self._runtime.probe_temporal():
            return BackendControlResult(job, error="Temporal Worker 当前不可达，控制请求未送达，请检查连接后重试。")
        try:
            from app.agents.orchestration.temporal.client import signal_logical_effects_workflow

            await signal_logical_effects_workflow(job.job_id, signal, arg)
            return BackendControlResult(job)
        except Exception as exc:  # noqa: BLE001
            monitor_logger.warning(
                "Temporal 副作用逻辑计划控制失败",
                event_type="runtime_control_failure",
                category="external_service",
                code="TEMPORAL_LOGICAL_EFFECTS_CONTROL_FAILED",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
            return BackendControlResult(job, error="Temporal Worker 控制请求失败，任务状态未改变，请检查连接后重试。")

    async def cancel(self, job: Job | None, keep_completed: bool = True) -> BackendControlResult | None:
        result = await self._signal(job, "cancel_request", keep_completed)
        if result is None or result.error or job is None or self._terminal(job):
            return result
        transition(job, JobStatus.CANCELLED)
        job.updated_at = time.time()
        await self._runtime.store.save_job(job)
        return BackendControlResult(job, release_capacity=True)

    async def pause(self, job: Job | None) -> BackendControlResult | None:
        return await self._transition_after_signal(job, "pause", JobStatus.RUNNING, JobStatus.PAUSED)

    async def resume(self, job: Job | None) -> BackendControlResult | None:
        return await self._transition_after_signal(job, "resume", JobStatus.PAUSED, JobStatus.RUNNING)

    async def approve(self, job: Job, node_id: str, approved: bool) -> BackendControlResult | None:
        return await self._signal(job, "approve_task", {"node_id": str(node_id), "approved": bool(approved)})

    async def _transition_after_signal(
        self, job: Job | None, signal: str, source: JobStatus, target: JobStatus
    ) -> BackendControlResult | None:
        result = await self._signal(job, signal)
        if result is None or result.error or job is None:
            return result
        if job.status == source:
            transition(job, target)
            job.updated_at = time.time()
            await self._runtime.store.save_job(job)
        return BackendControlResult(job)

    @staticmethod
    def _terminal(job: Job) -> bool:
        return job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
