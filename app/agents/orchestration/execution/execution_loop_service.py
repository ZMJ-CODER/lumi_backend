"""进程内 DAG 执行循环与终态收敛。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.orchestration.execution.validation import DagValidationError
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state_machine.errors import classify_error
from app.platform.runtime.deadline import DeadlineExceeded


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
        # 任务级预算（方案 §deadline）：后台任务**脱离**请求作用域，装上自己的绝对截止
        # 时间。两个方向都要修：
        # * 请求预算不该管后台任务的寿命（SSE 断开后请求预算可能只剩几秒）；
        # * 后台任务此前完全没有上限（没有请求上下文时剩余预算是 inf），一个卡住的
        #   Provider 调用能把 worker 永久占住。
        # 每次 `run` 重新锚定 = "一段执行"的预算，暂停/等审批不消耗它（否则审批慢一点
        # 就把任务判死）。配置为 0 时仍然 clear（"不限制"必须是明确的）。
        deadline_token = self._install_job_deadline(job_id)
        try:
            await self._run_loop(job_id, llm_api_key=llm_api_key, llm_config=llm_config)
        finally:
            if deadline_token is not None:
                deadline_token.reset()

    @staticmethod
    def _install_job_deadline(job_id: str):
        """装上任务预算并返回还原凭证（异常一律不影响执行）。"""
        try:
            from app.platform.runtime.deadline import install_job_deadline

            return install_job_deadline(source="job.execution_loop")
        except Exception as exc:  # noqa: BLE001 - 预算装配失败不能阻止任务执行
            logger.debug("任务预算装配失败 {}: {}", job_id, str(exc)[:120])
            return None

    async def _run_loop(self, job_id: str, *, llm_api_key: str | None, llm_config: dict | None) -> None:
        try:
            job = await self._store.get_job(job_id) or self._live_jobs.get(job_id)
            if job is None:
                return
            # v2 观测：进入 Agent/节点执行（指标默认关闭时零开销）。
            try:
                from app.observability.observability import inc_agent_invoked

                routing = job.routing if isinstance(job.routing, dict) else {}
                inc_agent_invoked(
                    str(routing.get("execution_policy") or ""),
                    str(routing.get("complexity") or ""),
                )
            except Exception:  # noqa: BLE001 - 观测失败不影响执行
                pass
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
        except DeadlineExceeded as exc:
            # 任务预算耗尽：**稳定错误码 + 可行动文案**。不能只落成泛化的"任务执行超时"，
            # 否则运维分不清"上游慢"还是"这个任务本来就该被预算停掉"。
            logger.warning(
                "任务预算耗尽，已停止 {}: source={} remaining={}",
                job_id,
                exc.source or "-",
                round(float(exc.remaining or 0.0), 3),
            )
            await self._job_errors.fail(
                job_id,
                "任务执行预算已耗尽，已安全停止。可将任务拆小后重试，"
                "或由运维调大 JOB_DEADLINE_SECONDS。",
                error_code="JOB_DEADLINE_EXCEEDED",
            )
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
                        # The finalizer needs to distinguish a deliverable
                        # answer from a retrieval observation.  In particular,
                        # returning the one ``web_search`` node verbatim
                        # bypasses synthesis and exposes search snippets as
                        # the assistant's answer.
                        "tool": str(value.get("tool") or ""),
                    }
                )
        # A rolling logical plan keeps completed batches outside ``job.nodes``
        # to avoid bloating the mutable snapshot.  At delivery time the whole
        # completed result set must nevertheless be restored; otherwise only
        # the final frontier is summarized and earlier batches disappear.
        pointer = (job.routing or {}).get("logical_plan") if isinstance(job.routing, dict) else None
        if isinstance(pointer, dict) and pointer.get("plan_id"):
            try:
                from app.agents.orchestration.planning.logical_plan import load_logical_plan
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
                            "tool": str(value.get("tool") or ""),
                        })
                if restored:
                    results = restored
            except Exception as exc:  # noqa: BLE001
                logger.debug("恢复逻辑计划完整结果失败 {}: {}", job.job_id, exc)
        # A lone retrieval result is evidence, not the final response.  It
        # must pass through the final-answer agent so that it is compared,
        # summarized and presented as an answer rather than as raw tool
        # output.  Keep the zero-extra-LLM fast path for direct answers and
        # deterministic tools.
        retrieval_tools = {"web_search", "web_fetch", "kb_search", "query_knowledge"}
        needs_synthesis = any(str(item.get("tool") or "") in retrieval_tools for item in results)
        if len(results) == 1 and not needs_synthesis:
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
                retrieval_tools = {"web_search", "web_fetch", "kb_search", "query_knowledge"}
                if any(str(item.get("tool") or "") in retrieval_tools for item in results):
                    # Retrieval observations are not a safe deterministic
                    # fallback: they contain snippets, prompt-shaped text and
                    # provider formatting.  Never dump them into the user
                    # bubble when the synthesis model is unavailable.
                    fallback = "已完成资料检索，但当前归纳服务暂时不可用；请稍后重试以获取整理后的结论。"
                else:
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
                # Synthesis is presentation-only.  Even an unclassified local
                # formatter failure must not leave a completed job without a
                # deliverable final_answer.
                fallback = "\n\n".join(
                    f"{item['title']}：{item['content']}"
                    for item in results
                    if str(item.get("content") or "").strip()
                )[:60000]
                job.result = {
                    "final_answer": fallback or "任务已完成，但未产生可展示内容。",
                    "delivery_status": "degraded",
                    "delivery_error_code": code,
                    "delivery_error": message,
                }
                await self._store.save_job(job)
