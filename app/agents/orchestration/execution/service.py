"""将任务规格执行为持久化任务结果的应用服务。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from lumi_execution import JobExecutionResult, TaskExecutionEngine

from app.agents.orchestration.execution.lifecycle import (
    ApplicationExecutionControl,
    ApplicationNodeLifecycle,
    TERMINAL_TASK_STATUSES,
    prior_node_result,
)
from app.agents.orchestration.execution.node import ApplicationTaskNodeExecutor
from app.agents.orchestration.job_contract import freeze_job_spec
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.core.config import settings


class ApplicationTaskExecutionService:
    """Drive a Job to completion or an explicit durable suspension state."""

    def __init__(self, *, store: Any, workers: Mapping[str, Any], review: Any) -> None:
        self._store = store
        self._workers = workers
        self._review = review

    async def execute(
        self,
        job: Job,
        *,
        concurrency: int,
        llm_api_key: str | None = None,
        llm_config: dict | None = None,
        on_waiting_resources: Callable[[Job], Awaitable[None]] | None = None,
        ensure_active_capacity: Callable[[Job], Awaitable[bool]] | None = None,
    ) -> JobExecutionResult:
        job = await self._load_or_create(job)
        executor = ApplicationTaskNodeExecutor(
            job=job,
            workers=self._workers,
            review=self._review,
            store=self._store,
            llm_api_key=llm_api_key,
            llm_config=llm_config,
        )
        lifecycle = ApplicationNodeLifecycle(job, self._store)
        while True:
            outcome = await self._run_once(job, executor, lifecycle, concurrency)
            if outcome.status != "waiting_resources":
                await self._apply_job_outcome(job, outcome)
                return outcome
            await self._enter_resource_wait(job, on_waiting_resources)
            if not await self._wait_for_resources(job, executor, ensure_active_capacity):
                paused = outcome.model_copy(update={"status": "paused", "error": job.error})
                await self._apply_job_outcome(job, paused)
                return paused

    async def _run_once(self, job, executor, lifecycle, concurrency) -> JobExecutionResult:
        spec = freeze_job_spec(job)
        prior = tuple(prior_node_result(node) for node in job.nodes if node.status in TERMINAL_TASK_STATUSES)
        engine = TaskExecutionEngine(
            executor=executor,
            concurrency=concurrency,
            control=ApplicationExecutionControl(self._store),
            lifecycle=lifecycle,
        )
        return await engine.run(spec, prior_results=prior)

    async def _load_or_create(self, job: Job) -> Job:
        stored = await self._store.get_job(job.job_id)
        if stored is not None:
            return stored
        await self._store.create_job(job)
        return job

    async def _enter_resource_wait(self, job: Job, callback) -> None:
        job.routing = dict(job.routing or {})
        job.routing.setdefault("waiting_resources_started_at", time.time())
        if not job.routing.get("admission_released_while_waiting"):
            job.routing["admission_released_while_waiting"] = True
            await self._store.save_job(job)
            if callback is not None:
                await callback(job)
        await self._store.save_job(job)

    async def _wait_for_resources(self, job, executor, ensure_active_capacity) -> bool:
        started = float((job.routing or {}).get("waiting_resources_started_at") or time.time())
        waiting = [node for node in job.nodes if (node.metadata or {}).get("waiting_resources")]
        while time.time() - started < max(60, int(settings.AGENT_WAITING_RESOURCES_TIMEOUT_SECONDS)):
            if job.status in {JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.PAUSED}:
                return False
            if await executor.resources_available(waiting):
                if ensure_active_capacity is not None and not await ensure_active_capacity(job):
                    await asyncio.sleep(1)
                    continue
                for node in waiting:
                    node.metadata = dict(node.metadata or {})
                    node.metadata.pop("waiting_resources", None)
                    node.status = TaskStatus.PENDING
                    node.error = node.error_code = None
                job.status = JobStatus.RUNNING
                job.routing.pop("waiting_resources_started_at", None)
                await self._store.save_job(job)
                return True
            await asyncio.sleep(0.1 if self._store.__class__.__name__ == "InMemoryStateStore" else 1)
        job.status = JobStatus.PAUSED
        job.error = "写资源协调服务在等待时限内未恢复，任务已暂停，请恢复服务后手动继续。"
        return False

    async def _apply_job_outcome(self, job: Job, outcome: JobExecutionResult) -> None:
        job.status = JobStatus(outcome.status)
        job.result = outcome.result
        job.error = outcome.error
        job.updated_at = time.time()
        await self._store.save_job(job)
