"""静态 DAG 灰度范围的 Temporal 后端。"""

from __future__ import annotations

import time

from app.agents.orchestration.backends.contracts import BackendControlResult
from app.agents.orchestration.job_contract import freeze_job_spec
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.state_machine.transitions import transition
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


class TemporalStaticBackend:
    """External-worker backend for the static, reviewable DAG subset."""

    name = "temporal_static"

    def __init__(self, runtime: RuntimeGateway) -> None:
        self._runtime = runtime

    async def submit(
        self, job: Job, llm_api_key: str | None, llm_config: dict | None = None
    ) -> None:
        freeze_job_spec(job)
        await self._runtime.submit_static(job, llm_api_key, llm_config)

    @staticmethod
    def _owns(job: Job | None) -> bool:
        return RuntimeGateway.is_static_job(job)

    async def _ready(self, job: Job | None) -> bool:
        return bool(job and self._owns(job) and await self._runtime.probe_temporal())

    async def cancel(
        self, job: Job | None, keep_completed: bool = True
    ) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        assert job is not None
        if not await self._ready(job):
            self._unavailable("取消", job, "TEMPORAL_STATIC_UNAVAILABLE")
            return BackendControlResult(job, error="Temporal Worker 当前不可达，取消请求未送达，请检查连接后重试。")
        if self._terminal(job):
            return BackendControlResult(job)
        try:
            from app.agents.orchestration.temporal.client import cancel_agent_workflow

            await cancel_agent_workflow(job.job_id, keep_completed)
            transition(job, JobStatus.CANCELLED)
            job.updated_at = time.time()
            await self._runtime.store.save_job(job)
            return BackendControlResult(job, release_capacity=True)
        except Exception as exc:  # noqa: BLE001
            return self._failure("取消", job, exc)

    async def pause(self, job: Job | None) -> BackendControlResult | None:
        return await self._signal_state(job, "pause")

    async def resume(self, job: Job | None) -> BackendControlResult | None:
        return await self._signal_state(job, "resume")

    async def approve(self, job: Job, node_id: str, approved: bool) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        if not await self._ready(job):
            return BackendControlResult(job, error="Temporal Worker 当前不可达，审批信号未送达，请检查连接后重试。")
        try:
            from app.agents.orchestration.temporal.client import approve_agent_workflow

            await approve_agent_workflow(job.job_id, node_id, approved)
            return BackendControlResult(job)
        except Exception as exc:  # noqa: BLE001
            return self._failure("审批", job, exc)

    async def _signal_state(self, job: Job | None, action: str) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        assert job is not None
        if not await self._ready(job):
            return BackendControlResult(job, error=f"Temporal Worker 当前不可达，{action}请求未送达，请检查连接后重试。")
        try:
            if action == "pause" and job.status == JobStatus.RUNNING:
                from app.agents.orchestration.temporal.client import pause_agent_workflow

                await pause_agent_workflow(job.job_id)
                transition(job, JobStatus.PAUSED)
            elif action == "resume" and job.status == JobStatus.PAUSED:
                from app.agents.orchestration.temporal.client import resume_agent_workflow

                await resume_agent_workflow(job.job_id)
                transition(job, JobStatus.RUNNING)
            else:
                return BackendControlResult(job)
            job.updated_at = time.time()
            await self._runtime.store.save_job(job)
            return BackendControlResult(job)
        except Exception as exc:  # noqa: BLE001
            return self._failure("暂停" if action == "pause" else "恢复", job, exc)

    @staticmethod
    def _terminal(job: Job) -> bool:
        return job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}

    def _unavailable(self, action: str, job: Job, code: str) -> None:
        monitor_logger.warning(
            f"Temporal 静态 DAG {action}未发送：运行器不可达",
            event_type="runtime_control_failure",
            category="external_service",
            code=code,
            context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
        )

    def _failure(self, action: str, job: Job, exc: Exception) -> BackendControlResult:
        code_by_action = {
            "取消": "TEMPORAL_STATIC_CANCEL_FAILED",
            "暂停": "TEMPORAL_STATIC_PAUSE_FAILED",
            "恢复": "TEMPORAL_STATIC_RESUME_FAILED",
            "审批": "TEMPORAL_STATIC_APPROVE_FAILED",
        }
        monitor_logger.warning(
            f"Temporal 静态 DAG {action}控制失败",
            event_type="runtime_control_failure",
            category="external_service",
            code=code_by_action[action],
            context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
            metadata={"error": str(exc)},
        )
        return BackendControlResult(job, error=f"Temporal Worker {action}请求失败，任务状态未改变，请检查连接后重试。")
