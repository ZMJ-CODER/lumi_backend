"""由应用任务执行服务驱动的进程内后端。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import time

from app.agents.orchestration.backends.contracts import BackendControlResult
from app.agents.orchestration.job_contract import freeze_job_spec
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.state import StateStore
from app.agents.orchestration.state_machine.transitions import transition


class LegacyDagBackend:
    """Submit a job to the in-process application execution service."""

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
        freeze_job_spec(job)
        await self._store.create_job(job)
        self._live_jobs[job.job_id] = job
        if llm_api_key:
            self._api_keys[job.job_id] = llm_api_key
        self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))

    async def cancel(
        self, job: Job | None, keep_completed: bool = True
    ) -> BackendControlResult | None:
        if job is None:
            return None
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
            return BackendControlResult(job)
        transition(job, JobStatus.CANCELLED)
        job.updated_at = time.time()
        for node in job.nodes:
            if node.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RETRYING}:
                node.status = TaskStatus.CANCELLED
                node.error = "任务被用户终止"
                node.error_code = "JOB_CANCELLED"
            elif node.status == TaskStatus.RUNNING:
                node.status = TaskStatus.INTERRUPTED
                node.error = "任务被用户终止"
                node.error_code = "JOB_CANCELLED"
        await self._store.save_job(job)
        # 取消 Legacy 后台协程，避免长耗时 Activity 在测试/生产中继续占用
        # worker、工具互斥锁和事件循环。ExecutionLoopService 会在取消异常中
        # 统一收敛已运行节点为 interrupted。
        task = self._tasks.get(job.job_id)
        if task is not None and not task.done():
            task.cancel()
            # 等待执行循环完成状态收敛，控制 API 返回时快照已可读；
            # 不把后台取消异常传播给调用方。
            with suppress(asyncio.CancelledError):
                await task
            settled = await self._store.get_job(job.job_id)
            if settled is not None and settled.status == JobStatus.CANCELLED:
                for node in settled.nodes:
                    if node.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RETRYING}:
                        node.status = TaskStatus.CANCELLED
                        node.error = "任务被用户终止"
                        node.error_code = "JOB_CANCELLED"
                    elif node.status == TaskStatus.RUNNING:
                        node.status = TaskStatus.INTERRUPTED
                        node.error = "任务被用户终止"
                        node.error_code = "JOB_CANCELLED"
                await self._store.save_job(settled)
        return BackendControlResult(job, release_capacity=True)

    async def pause(self, job: Job | None) -> BackendControlResult | None:
        if job is None:
            return None
        if job.status == JobStatus.RUNNING:
            transition(job, JobStatus.PAUSED)
            job.updated_at = time.time()
            await self._store.save_job(job)
        return BackendControlResult(job)

    async def resume(self, job: Job | None) -> BackendControlResult | None:
        if job is None:
            return None
        if job.status == JobStatus.PAUSED:
            transition(job, JobStatus.RUNNING)
            job.updated_at = time.time()
            await self._store.save_job(job)
            self._live_jobs[job.job_id] = job
            if job.job_id not in self._tasks or self._tasks[job.job_id].done():
                self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))
        return BackendControlResult(job)

    async def approve(self, job: Job, node_id: str, approved: bool) -> BackendControlResult:
        """Resume after facade-level approval validation succeeds."""
        if approved:
            self._live_jobs[job.job_id] = job
            if job.job_id not in self._tasks or self._tasks[job.job_id].done():
                self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))
        return BackendControlResult(job)
