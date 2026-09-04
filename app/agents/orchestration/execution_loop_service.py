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
            # The runtime-neutral engine always returns an aggregate
            # ``outputs`` envelope.  It is not yet a user-facing answer, so
            # synthesize whenever the terminal snapshot lacks
            # ``final_answer`` rather than checking for an empty result.
            if (
                job
                and job.status == JobStatus.COMPLETED
                and (
                    not isinstance(job.result, dict)
                    or not str((job.result or {}).get("final_answer") or "").strip()
                )
            ):
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
        # A rolling logical plan keeps completed batches outside ``job.nodes``
        # to avoid bloating the mutable snapshot.  At delivery time the whole
        # completed result set must nevertheless be restored; otherwise only
        # the final frontier is summarized and earlier batches disappear.
        pointer = (job.routing or {}).get("logical_plan") if isinstance(job.routing, dict) else None
        if isinstance(pointer, dict) and pointer.get("plan_id"):
            try:
                from app.agents.orchestration.logical_plan import load_logical_plan
                from app.agents.orchestration.execution.lineage import resolve_result_ref

                plan = await load_logical_plan(job.user_id, str(pointer["plan_id"]))
                records = (plan or {}).get("nodes") or {}
                restored = []
                for node_id in (plan or {}).get("order") or []:
                    record = records.get(str(node_id))
                    if not isinstance(record, dict) or str(record.get("status") or "") != "completed":
                        continue
                    ref = record.get("result_ref")
                    value = await resolve_result_ref(job.user_id, ref) if isinstance(ref, dict) else None
                    if not isinstance(value, dict):
                        continue
                    content = str(value.get("content") or value.get("output") or value.get("answer") or "").strip()
                    if content:
                        source = record.get("node") or {}
                        restored.append({
                            "agent": str(source.get("agent") or ""),
                            "title": str(source.get("name") or node_id),
                            "content": content[:30000],
                        })
                if restored:
                    results = restored
            except Exception as exc:  # noqa: BLE001
                logger.debug("恢复逻辑计划完整结果失败 {}: {}", job.job_id, exc)
        if len(results) == 1:
            job.result = {"final_answer": results[0]["content"]}
            await self._store.save_job(job)
            return
        if not results:
            return
        # 全部为确定性原子工具结果时，按原始节点顺序直接交付，不能因为
        # “汇总排版”再调用模型。这样计算、时间等只读 DAG 在模型网络短暂
        # 不可用时依然是完整可交付的；含文本分析节点的任务仍走 LLM 汇总。
        deterministic_tools = {"Calculator", "DateTime"}
        if all(
            str((node.result or {}).get("tool") or "") in deterministic_tools
            for node in job.nodes
            if (node.result or {}).get("content") or (node.result or {}).get("output")
        ):
            job.result = {
                "final_answer": "\n".join(str(item["content"]) for item in results),
            }
            await self._store.save_job(job)
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
                # Final formatting is an optional delivery step.  A provider
                # outage after every node succeeded must not turn a completed
                # large/batched DAG into a failed job or discard its outputs.
                # Return a bounded deterministic envelope and expose the
                # formatting degradation separately for the UI/telemetry.
                fallback = "\n\n".join(
                    f"{item['title']}：{item['content']}"
                    for item in results
                    if str(item.get("content") or "").strip()
                )[:60000]
                job.status = JobStatus.COMPLETED
                job.error = None
                job.result = {
                    "final_answer": fallback,
                    "delivery_status": "degraded",
                    "delivery_error_code": code,
                    "delivery_error": message,
                }
                await self._store.save_job(job)
            else:
                logger.debug("legacy DAG 最终答案汇总失败 {}: {}", job.job_id, exc)
