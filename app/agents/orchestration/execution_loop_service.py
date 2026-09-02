"""进程内 DAG 执行循环与终态收敛。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.orchestration.execution.validation import DagValidationError
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state_machine.errors import classify_error


class ExecutionLoopService:
    """Run one legacy job while delegating policy decisions to the facade."""

    _TERMINAL = frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.WAITING_APPROVAL,
            JobStatus.WAITING_RESOURCES,
            JobStatus.PAUSED,
        }
    )

    def __init__(
        self,
        *,
        store: Any,
        workers: dict,
        review: Any,
        job_errors: Any,
        finalizer: Any,
        live_jobs: dict[str, Job],
        tasks: dict[str, asyncio.Task],
        api_keys: dict[str, str],
        llm_configs: dict[str, dict],
        plan_context: dict[str, dict],
        context_getter: Callable[[str], dict],
        continue_manifest: Callable[[Job], Awaitable[bool]],
        continue_logical_plan: Callable[[Job], Awaitable[bool]],
        maybe_replan: Callable[[Job, str | None], Awaitable[bool]],
        node_concurrency: int,
        suspend_capacity: Callable[[Job], Awaitable[None]] | None = None,
        ensure_active_capacity: Callable[[Job], Awaitable[bool]] | None = None,
        task_execution_service: Any,
    ) -> None:
        self._store = store
        self._workers = workers
        self._review = review
        self._job_errors = job_errors
        self._finalizer = finalizer
        self._live_jobs = live_jobs
        self._tasks = tasks
        self._api_keys = api_keys
        self._llm_configs = llm_configs
        self._plan_context = plan_context
        self._context_getter = context_getter
        self._continue_manifest = continue_manifest
        self._continue_logical_plan = continue_logical_plan
        self._maybe_replan = maybe_replan
        self._node_concurrency = node_concurrency
        self._suspend_capacity = suspend_capacity or (lambda _job: self._noop())
        self._ensure_active_capacity = ensure_active_capacity or (lambda _job: self._allow())
        self._task_execution_service = task_execution_service

    @staticmethod
    async def _noop() -> None:
        return None

    @staticmethod
    async def _allow() -> bool:
        return True

    async def run(self, job_id: str) -> None:
        llm_api_key = self._api_keys.get(job_id)
        llm_config = self._llm_configs.get(job_id) or self._context_getter(job_id).get(
            "llm_config"
        )
        try:
            job = await self._store.get_job(job_id) or self._live_jobs.get(job_id)
            if job is None:
                return
            while True:
                await self._task_execution_service.execute(
                    job,
                    concurrency=self._node_concurrency,
                    llm_api_key=llm_api_key,
                    llm_config=llm_config,
                    on_waiting_resources=self._suspend_capacity,
                    ensure_active_capacity=self._ensure_active_capacity,
                )
                job = await self._store.get_job(job_id) or job
                self._live_jobs[job_id] = job
                if await self._continue_manifest(job):
                    job = await self._store.get_job(job_id) or job
                    self._live_jobs[job_id] = job
                    continue
                if await self._continue_logical_plan(job):
                    job = await self._store.get_job(job_id) or job
                    self._live_jobs[job_id] = job
                    continue
                if not await self._maybe_replan(job, llm_api_key):
                    break
                job = await self._store.get_job(job_id) or job
                self._live_jobs[job_id] = job
                if job.status in self._TERMINAL:
                    break
            job = await self._store.get_job(job_id)
            if job and job.status not in self._TERMINAL:
                job = await self._job_errors.ensure_failed(
                    job, "办公任务未能收敛，已自动停止。"
                )
            if job and job.status == JobStatus.COMPLETED and not job.result:
                await self._synthesize_final_answer(job)
        except DagValidationError as exc:
            logger.error("任务 DAG 非法 {}: {}", job_id, exc)
            await self._job_errors.fail(job_id, exc, error_code="DAG_VALIDATION_ERROR")
        except asyncio.CancelledError:
            logger.info("任务后台执行被取消: {}", job_id)
            await self._job_errors.interrupt(job_id, "任务后台执行被取消")
        except Exception as exc:  # noqa: BLE001
            logger.error("任务执行异常 {}: {}", job_id, exc)
            info = classify_error(exc)
            await self._job_errors.fail(job_id, exc, error_code=info.code)
        finally:
            self._tasks.pop(job_id, None)
            finished = await self._store.get_job(job_id)
            suspended = bool(finished and finished.status in {
                JobStatus.WAITING_APPROVAL, JobStatus.PAUSED,
            })
            if suspended:
                await self._finalizer.suspend_capacity(finished)
            if not suspended:
                self._api_keys.pop(job_id, None)
                self._llm_configs.pop(job_id, None)
                self._plan_context.pop(job_id, None)
                self._live_jobs.pop(job_id, None)
            try:
                finished = finished or await self._store.get_job(job_id)
                await self._finalizer.finalize(finished)
            except Exception as exc:  # noqa: BLE001
                logger.debug("释放办公任务准入槽失败 {}: {}", job_id, exc)

    async def _synthesize_final_answer(self, job: Job) -> None:
        results = []
        for node in job.nodes:
            value = node.result or {}
            content = value.get("content") or value.get("output") or ""
            if content:
                results.append(
                    {
                        "agent": node.agent,
                        "title": node.name or node.agent,
                        "content": str(content)[:30000],
                    }
                )
        if len(results) == 1:
            job.result = {"final_answer": results[0]["content"]}
            await self._store.save_job(job)
            return
        if not results:
            return
        try:
            from app.agents.orchestration.temporal.activities import (
                synthesize_final_answer_activity,
            )

            synthesized = await synthesize_final_answer_activity(
                {
                    "user_id": job.user_id,
                    "job_id": job.job_id,
                    "request": job.request,
                    "nodes": results,
                    "presentation_preferences": self._context_getter(job.job_id).get(
                        "presentation_preferences", ""
                    ),
                }
            )
            if synthesized.get("final_answer"):
                job.result = synthesized
                await self._store.save_job(job)
        except Exception as exc:  # noqa: BLE001
            from app.agents.skills.recovery import (
                classify_model_error,
                is_terminal_model_error_code,
            )

            code, message = classify_model_error(exc)
            if is_terminal_model_error_code(code):
                job.status = JobStatus.FAILED
                job.error = message
                job.result = {"error_code": code, "message": message}
                await self._store.save_job(job)
            else:
                logger.debug("legacy DAG 最终答案汇总失败 {}: {}", job.job_id, exc)
