"""Execution backend adapters used by the orchestration facade.

The planner produces a Job; a backend owns the mechanics of submitting that
Job to a runtime.  Control signals are intentionally not mixed into this
first slice so legacy state transitions and Temporal signals remain unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time
from typing import Protocol

from app.agents.orchestration.models import Job
from app.agents.orchestration.models import JobStatus, TaskStatus
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.state import StateStore
from app.agents.orchestration.state_machine.transitions import transition
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


class ExecutionBackend(Protocol):
    """Runtime lifecycle contract shared by the orchestration facade."""

    name: str

    async def submit(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        ...

    async def cancel(self, job: Job | None, keep_completed: bool = True):
        ...

    async def pause(self, job: Job | None):
        ...

    async def resume(self, job: Job | None):
        ...

    async def approve(self, job: Job, node_id: str, approved: bool):
        ...


@dataclass(frozen=True, slots=True)
class BackendControlResult:
    """Result of a backend control operation.

    ``handled=False`` lets the facade try the legacy fallback when Temporal is
    unavailable. ``release_capacity`` is explicit because a terminal snapshot
    should not release an admission slot twice.
    """

    job: Job
    handled: bool = True
    release_capacity: bool = False
    error: str | None = None


class LegacyDagBackend:
    """Submit a Job to the in-process asyncio DAG runner."""

    name = "legacy"

    def __init__(
        self,
        *,
        store: StateStore,
        live_jobs: dict[str, Job],
        tasks: dict[str, asyncio.Task],
        api_keys: dict[str, str],
        run_job: Callable[[str], Awaitable[None]],
    ) -> None:
        self._store = store
        self._live_jobs = live_jobs
        self._tasks = tasks
        self._api_keys = api_keys
        self._run_job = run_job

    async def submit(self, job: Job, llm_api_key: str | None) -> None:
        await self._store.create_job(job)
        self._live_jobs[job.job_id] = job
        if llm_api_key:
            self._api_keys[job.job_id] = llm_api_key
        self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))

    async def cancel(
        self,
        job: Job | None,
        keep_completed: bool = True,
    ) -> BackendControlResult | None:
        if job is None or job.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        ):
            return BackendControlResult(job, release_capacity=False) if job else None
        transition(job, JobStatus.CANCELLED)
        job.updated_at = time.time()
        if not keep_completed:
            for node in job.nodes:
                if node.status in (
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.RETRYING,
                ):
                    node.status = TaskStatus.CANCELLED
                    node.error = "任务被用户终止"
        await self._store.save_job(job)
        return BackendControlResult(job, release_capacity=True)

    async def pause(self, job: Job | None) -> BackendControlResult | None:
        if job is None or job.status != JobStatus.RUNNING:
            return BackendControlResult(job) if job else None
        transition(job, JobStatus.PAUSED)
        job.updated_at = time.time()
        await self._store.save_job(job)
        return BackendControlResult(job)

    async def resume(self, job: Job | None) -> BackendControlResult | None:
        if job is None or job.status != JobStatus.PAUSED:
            return BackendControlResult(job) if job else None
        transition(job, JobStatus.RUNNING)
        job.updated_at = time.time()
        await self._store.save_job(job)
        self._live_jobs[job.job_id] = job
        if job.job_id not in self._tasks or self._tasks[job.job_id].done():
            self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))
        return BackendControlResult(job)

    async def approve(
        self,
        job: Job,
        node_id: str,
        approved: bool,
    ) -> BackendControlResult:
        """Resume the in-process runner after facade-level approval checks."""
        if approved:
            self._live_jobs[job.job_id] = job
            if job.job_id not in self._tasks or self._tasks[job.job_id].done():
                self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))
        return BackendControlResult(job)


class TemporalManifestBackend:
    """Submit an explicitly authorized rolling manifest to Temporal."""

    name = "manifest_temporal"

    def __init__(self, runtime: RuntimeGateway) -> None:
        self._runtime = runtime

    async def submit(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        await self._runtime.submit_manifest(job, llm_api_key, llm_config)

    async def _ready(self, job: Job | None) -> bool:
        return bool(
            job
            and RuntimeGateway.is_manifest_job(job)
            and await self._runtime.probe_temporal()
        )

    async def cancel(
        self,
        job: Job | None,
        keep_completed: bool = True,
    ) -> BackendControlResult | None:
        if not await self._ready(job):
            return None
        assert job is not None
        if job.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        ):
            return BackendControlResult(job)
        try:
            await self._runtime.cancel_manifest(job.job_id, keep_completed)
            transition(job, JobStatus.CANCELLED)
            job.updated_at = time.time()
            await self._runtime.store.save_job(job)
            return BackendControlResult(job, release_capacity=True)
        except Exception as exc:  # noqa: BLE001
            monitor_logger.warning(
                "Temporal 清单取消控制失败，回退 legacy",
                event_type="runtime_control_fallback",
                category="external_service",
                code="TEMPORAL_CANCEL_FALLBACK",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
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
            monitor_logger.warning(
                "Temporal 清单暂停控制失败，回退 legacy",
                event_type="runtime_control_fallback",
                category="external_service",
                code="TEMPORAL_PAUSE_FALLBACK",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
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
            monitor_logger.warning(
                "Temporal 清单恢复控制失败，回退 legacy",
                event_type="runtime_control_fallback",
                category="external_service",
                code="TEMPORAL_RESUME_FALLBACK",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
            return None

    async def approve(
        self,
        job: Job,
        node_id: str,
        approved: bool,
    ) -> BackendControlResult | None:
        # The rolling manifest workflow has no approval signal; its items are
        # pre-authorized by manifest construction. Let the facade use legacy
        # approval handling if a future manifest adds one.
        return None


class TemporalStaticBackend:
    """External worker backend for a deliberately narrow static-DAG subset."""

    name = "temporal_static"

    def __init__(self, runtime: RuntimeGateway) -> None:
        self._runtime = runtime

    async def submit(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        await self._runtime.submit_static(job, llm_api_key, llm_config)

    async def _ready(self, job: Job | None) -> bool:
        return bool(job and RuntimeGateway.is_static_job(job) and await self._runtime.probe_temporal())

    @staticmethod
    def _owns(job: Job | None) -> bool:
        return RuntimeGateway.is_static_job(job)

    async def cancel(
        self,
        job: Job | None,
        keep_completed: bool = True,
    ) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        assert job is not None
        if not await self._ready(job):
            monitor_logger.warning(
                "Temporal 静态 DAG 取消未发送：运行器不可达",
                event_type="runtime_control_failure",
                category="external_service",
                code="TEMPORAL_STATIC_UNAVAILABLE",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
            )
            return BackendControlResult(
                job,
                error="Temporal Worker 当前不可达，取消请求未送达，请检查连接后重试。",
            )
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
            return BackendControlResult(job)
        try:
            from app.agents.orchestration.temporal.client import cancel_agent_workflow

            await cancel_agent_workflow(job.job_id, keep_completed)
            transition(job, JobStatus.CANCELLED)
            job.updated_at = time.time()
            await self._runtime.store.save_job(job)
            return BackendControlResult(job, release_capacity=True)
        except Exception as exc:  # noqa: BLE001
            monitor_logger.warning(
                "Temporal 静态 DAG 取消控制失败，拒绝回退到进程内执行器",
                event_type="runtime_control_failure",
                category="external_service",
                code="TEMPORAL_STATIC_CANCEL_FAILED",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
            return BackendControlResult(
                job,
                error="Temporal Worker 取消请求失败，任务状态未改变，请检查连接后重试。",
            )

    async def pause(self, job: Job | None) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        assert job is not None
        if not await self._ready(job):
            return BackendControlResult(
                job,
                error="Temporal Worker 当前不可达，暂停请求未送达，请检查连接后重试。",
            )
        try:
            if job.status == JobStatus.RUNNING:
                from app.agents.orchestration.temporal.client import pause_agent_workflow

                await pause_agent_workflow(job.job_id)
                transition(job, JobStatus.PAUSED)
                job.updated_at = time.time()
                await self._runtime.store.save_job(job)
            return BackendControlResult(job)
        except Exception as exc:  # noqa: BLE001
            monitor_logger.warning(
                "Temporal 静态 DAG 暂停控制失败",
                event_type="runtime_control_failure",
                category="external_service",
                code="TEMPORAL_STATIC_PAUSE_FAILED",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
            return BackendControlResult(
                job,
                error="Temporal Worker 暂停请求失败，任务状态未改变，请检查连接后重试。",
            )

    async def resume(self, job: Job | None) -> BackendControlResult | None:
        if not self._owns(job):
            return None
        assert job is not None
        if not await self._ready(job):
            return BackendControlResult(
                job,
                error="Temporal Worker 当前不可达，恢复请求未送达，请检查连接后重试。",
            )
        try:
            if job.status == JobStatus.PAUSED:
                from app.agents.orchestration.temporal.client import resume_agent_workflow

                await resume_agent_workflow(job.job_id)
                transition(job, JobStatus.RUNNING)
                job.updated_at = time.time()
                await self._runtime.store.save_job(job)
            return BackendControlResult(job)
        except Exception as exc:  # noqa: BLE001
            monitor_logger.warning(
                "Temporal 静态 DAG 恢复控制失败",
                event_type="runtime_control_failure",
                category="external_service",
                code="TEMPORAL_STATIC_RESUME_FAILED",
                context=MonitorContext(job_id=job.job_id, runtime=self.name, component="execution_backend"),
                metadata={"error": str(exc)},
            )
            return BackendControlResult(
                job,
                error="Temporal Worker 恢复请求失败，任务状态未改变，请检查连接后重试。",
            )

    async def approve(
        self,
        job: Job,
        node_id: str,
        approved: bool,
    ) -> BackendControlResult | None:
        # Static candidates exclude approval nodes. Keep this explicit so a
        # future planner change cannot silently weaken the approval boundary.
        return BackendControlResult(job) if self._owns(job) else None
