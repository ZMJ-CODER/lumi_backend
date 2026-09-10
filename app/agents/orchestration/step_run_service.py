"""step_confirm 计划优先任务的“单步执行”运行器（/jobs/{id}/resume action=run_next）。

routing 契约（与 execution_mode / job_run_view / step_sequence 同源）：
  - routing["execution_mode"]      : direct | step_confirm | auto_routine
  - routing["execution_state"]     : planning | waiting_run | running_step | waiting_next
                                      | waiting_approval | completed | failed | cancelled
  - routing["steps"]               : [{id,title,description,domain,status,result_ref,
                                       result_summary?,error?,index}, ...]
  - routing["current_step_index"]  : 下一步在 steps 中的下标（0-based）
  - routing["plan_revision"]       : 计划版本（run_next 校验一致性用）

本模块只负责“执行一步”这一动作本体：
  1) 用 step_resume.validate_resume_request 做前置校验（归属/状态/步骤/版本/幂等/
     工作区绑定/依赖就绪）；
  2) 由 steps[current_step_index] 定位步骤、在 Job.nodes 中找同 id 的 TaskNode；
  3) 复用 ApplicationTaskNodeExecutor（node.py）执行该节点、ApplicationNodeLifecycle
     （lifecycle.py）把 running/completed/failed/waiting_approval 等结果写回 Job
     快照与 store —— 与整 DAG 执行走同一条 node 执行/审批/结果回写路径；
  4) 结果回写：步骤 result_ref / step 状态推进 / canonical 状态
     （waiting_next | waiting_approval | completed | failed），并挂 admission 心跳；
  5) SSE 事件流对齐前端 RunNextEvent 契约（见模块尾部事件说明），终态后必发 done。

不触碰：Temporal 运行时、整 DAG 引擎（TaskExecutionEngine）与逻辑计划滚动窗口；
计划优先的 step_confirm 任务在进程内（legacy）运行，逐步骤由本模块驱动。
"""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable

from loguru import logger

from app.agents.orchestration.execution_mode import (
    SSE_EVENT_DONE,
    SSE_EVENT_STEP_COMPLETED,
    SSE_EVENT_STEP_STARTED,
    SSE_EVENT_TASK_COMPLETED,
    SSE_EVENT_TASK_FAILED,
    SSE_EVENT_TOOL_COMPLETED,
    SSE_EVENT_TOOL_STARTED,
    SSE_EVENT_WAITING_APPROVAL,
    SSE_EVENT_WAITING_NEXT,
)
from app.agents.orchestration.job_run_view import (
    canonical_state_for,
    run_view,
    waiting_next_payload,
)
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.step_resume import (
    ResumeCheckInput,
    validate_resume_request,
)
from app.repositories.job_repository import JobRepository

EVENT_STEP_STARTED = SSE_EVENT_STEP_STARTED
EVENT_TOOL_STARTED = SSE_EVENT_TOOL_STARTED
EVENT_TOOL_COMPLETED = SSE_EVENT_TOOL_COMPLETED
EVENT_STEP_COMPLETED = SSE_EVENT_STEP_COMPLETED
EVENT_WAITING_NEXT = SSE_EVENT_WAITING_NEXT
EVENT_WAITING_APPROVAL = SSE_EVENT_WAITING_APPROVAL
EVENT_TASK_COMPLETED = SSE_EVENT_TASK_COMPLETED
EVENT_TASK_FAILED = SSE_EVENT_TASK_FAILED
EVENT_DONE = SSE_EVENT_DONE


def locate_current_step(job: Job) -> tuple[dict | None, int, TaskNode | None, bool]:
    """定位当前应执行的步骤与对应 TaskNode。

    Returns:
        (step_dict, index, task_node, has_more_pending_steps)
    """
    routing = job.routing if isinstance(job.routing, dict) else {}
    steps = [dict(s) for s in routing.get("steps") or [] if isinstance(s, dict)]
    nodes_by_id = {node.id: node for node in job.nodes}
    index = int(routing.get("current_step_index") or 0)
    if index < 0:
        index = 0
    for candidate in range(index, len(steps)):
        step = steps[candidate]
        if str(step.get("status") or "pending") in {"pending", "running", "waiting_approval"}:
            step_id = str(step.get("id") or "")
            return step, candidate, nodes_by_id.get(step_id), candidate < len(steps) - 1
    return None, -1, None, False


def _step_dependencies_done(job: Job, node: TaskNode | None) -> bool:
    """当前步骤的全部 depends_on 依赖节点必须已完成才能放行单步执行。"""
    if node is None:
        return False
    if not node.depends_on:
        return True
    nodes_by_id = {item.id: item for item in job.nodes}
    for dep_id in node.depends_on:
        dep = nodes_by_id.get(dep_id)
        if dep is None or dep.status != TaskStatus.COMPLETED:
            return False
    return True


def _update_step_fields(job: Job, index: int, *, status: str = "", result_ref=None,
                        result_summary: str = "", error: str | None = None) -> None:
    """原地更新 routing.steps[index] 的展示字段（JSON-safe）。"""
    routing = dict(job.routing or {})
    steps = [dict(s) for s in routing.get("steps") or [] if isinstance(s, dict)]
    if 0 <= index < len(steps):
        step = dict(steps[index])
        if status:
            step["status"] = status
        if result_ref is not None:
            step["result_ref"] = result_ref
        elif status == "pending":
            step.pop("result_ref", None)
        if result_summary is not None:
            step["result_summary"] = str(result_summary or "")[:400]
        if error is not None:
            step["error"] = str(error or "")[:1000]
        steps[index] = step
    routing["steps"] = steps
    job.routing = routing


def _mark_step_status(job: Job, index: int, *, status: str) -> None:
    _update_step_fields(job, index, status=status)


def _result_summary(node_result: dict | None, fallback: str = "") -> str:
    """从节点结果取一段不超过 200 字的人可见摘要。"""
    value = node_result or {}
    display = value.get("display")
    if isinstance(display, dict) and str(display.get("completed") or "").strip():
        return str(display["completed"])[:200]
    content = str(
        value.get("content") or value.get("output") or value.get("answer") or ""
    ).strip()
    if content:
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        if first_line and len(first_line) <= 120:
            return first_line
        return (content[:96] + "…") if len(content) > 96 else content
    return str(fallback or "")[:200]


def _node_tool_hint(node: TaskNode) -> str:
    """该步骤对应的确定性工具名（无则空串，表示该步是纯推理/文本步骤）。"""
    params = node.params or {}
    return str(
        params.get("preferred_tool")
        or (params.get("inputs") or {}).get("tool")
        or params.get("tool")
        or ""
    ).strip()


def _node_tool_from_result(node_result: dict | None) -> str:
    value = node_result or {}
    return str(value.get("tool") or value.get("tool_name") or "").strip()


def _error_event(message: str, *, code: str, status: int = 400) -> dict:
    return {"type": "error", "message": message, "status": status, "code": code}


class StepRunService:
    """驱动计划优先任务“下一步”的进程内单步执行器。

    依赖注入全部走可替换回调/对象，便于 InMemory 单元测试与编排器装配。
    """

    def __init__(
        self,
        *,
        store: JobRepository,
        workers: dict,
        review: Any,
        finalizer: Any,
        llm_configs: dict[str, dict],
        ensure_active_capacity: Callable[[Job], Awaitable[bool]] | None = None,
        suspend_capacity: Callable[[Job], Awaitable[None]] | None = None,
        handle_escalation: Callable[[Job], Awaitable[bool]] | None = None,
        finalize_completed: Callable[[Job], Awaitable[None]] | None = None,
        finalize_failed: Callable[[Job], Awaitable[None]] | None = None,
        poll_interval: float = 0.12,
    ) -> None:
        self._store = store
        self._workers = workers
        self._review = review
        self._finalizer = finalizer
        self._llm_configs = llm_configs
        self._ensure_active_capacity = ensure_active_capacity or self._noop_true
        self._suspend_capacity = suspend_capacity or (lambda _job: self._noop())
        self._handle_escalation = handle_escalation or (lambda _job: self._noop_false())
        self._finalize_completed = finalize_completed
        self._finalize_failed = finalize_failed
        self._poll_interval = max(0.02, float(poll_interval))
        # 每任务一把执行锁，阻止两个 run_next 并发执行同一任务。
        self._run_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    async def _noop() -> None:
        return None

    @staticmethod
    async def _noop_true() -> bool:
        return True

    @staticmethod
    async def _noop_false() -> bool:
        return False

    # ── 对外流式入口 ─────────────────────────────────────────

    async def run_next_stream(
        self,
        *,
        job_id: str,
        expected_step_id: str = "",
        plan_revision: int | None = None,
        idempotency_key: str = "",
        workspace_bound: bool = True,
    ) -> AsyncGenerator[dict, None]:
        """执行“下一步”并逐事件产出 SSE 载荷（对齐前端 RunNextEvent）。"""
        lock = self._run_locks.setdefault(job_id, asyncio.Lock())
        if lock.locked():
            yield _error_event(
                "该任务正在执行上一步，请稍候再试",
                code="STEP_ALREADY_RUNNING",
                status=409,
            )
            return
        async with lock:
            async for event in self._locked_run_next(
                job_id=job_id,
                expected_step_id=expected_step_id,
                plan_revision=plan_revision,
                idempotency_key=idempotency_key,
                workspace_bound=workspace_bound,
            ):
                yield event

    async def _locked_run_next(
        self,
        *,
        job_id: str,
        expected_step_id: str,
        plan_revision: int | None,
        idempotency_key: str,
        workspace_bound: bool,
    ) -> AsyncGenerator[dict, None]:
        job = await self._store.get_job(job_id)
        if job is None:
            yield _error_event("任务不存在或状态已过期", code="JOB_NOT_FOUND", status=404)
            return

        step, index, node, _ = locate_current_step(job)
        if step is None or node is None:
            if canonical_state_for(job) in {"completed", "failed", "cancelled"}:
                view = run_view(job)
                yield self._task_terminal_event(job, view)
                yield self._done_event(job_id, view)
            else:
                view = run_view(job)
                yield _error_event("当前没有可执行的下一步（计划已无待运行步骤）", code="STEP_NOT_FOUND")
                yield self._done_event(job_id, view)
            return

        step_id = str(step.get("id") or node.id)
        effective_key = str(idempotency_key or "").strip() or f"run-next-{uuid.uuid4().hex}"
        dependencies_done = _step_dependencies_done(job, node)
        check = self._validate(
            job,
            current_step_id=step_id,
            expected_step_id=expected_step_id,
            requested_plan_revision=plan_revision,
            idempotency_key=effective_key,
            workspace_bound=workspace_bound,
            dependencies_done=dependencies_done,
        )
        if not check["ok"]:
            view = run_view(job)
            yield _error_event(check["reason"], code=check["code"])
            yield self._done_event(job_id, view)
            return

        # 幂等键登记（校验通过后立即占用，防止重放）。
        job.routing = dict(job.routing or {})
        seen = list(job.routing.get("seen_step_keys") or [])
        seen.append(effective_key)
        job.routing["seen_step_keys"] = seen[-50:]
        job.routing["execution_state"] = "running_step"
        if job.status in {JobStatus.PENDING, JobStatus.WAITING_RESOURCES}:
            job.status = JobStatus.RUNNING
        job.routing["current_step_index"] = index
        _mark_step_status(job, index, status="running")
        job.updated_at = time.time()
        await self._store.save_job(job)

        # 准入：计划优先任务提交时已释放槽位，执行单步前重新激活。
        job = await self._store.get_job(job_id) or job
        if not await self._ensure_active_capacity(job):
            await self._revert_to_waiting(job, index=index, idempotency_key=effective_key)
            view = run_view(job)
            yield _error_event(
                "办公执行容量已满，请稍后重试“运行下一步”",
                code="STEP_CAPACITY_UNAVAILABLE",
                status=429,
            )
            yield self._done_event(job_id, view)
            return

        current_plan_revision = int((job.routing or {}).get("plan_revision") or 1)
        requested_plan_revision = int(plan_revision or current_plan_revision)
        yield {
            "type": EVENT_STEP_STARTED,
            "job_id": job_id,
            "step_id": step_id,
            "step_index": int(index),
            "title": str(step.get("title") or "")[:200],
            "plan_revision": current_plan_revision,
        }

        # 单步内 process 事件序号单调递增（供前端去重/排序）。
        sequence = itertools.count(1)
        tool_name = _node_tool_hint(node)
        call_id = f"call-{step_id}"
        if tool_name:
            yield {
                "type": EVENT_TOOL_STARTED,
                "job_id": job_id,
                "step_id": step_id,
                "call_id": call_id,
                "tool": tool_name,
                "display": str(
                    (node.params or {}).get("step_title")
                    or (step.get("title") or "")
                )[:200],
            }

        holder: dict[str, Any] = {}
        async for event in self._execution_event_stream(
            job=job,
            node=node,
            step_id=step_id,
            job_id=job_id,
            sequence=sequence,
            holder=holder,
        ):
            yield event
        outcome = holder.get("outcome")
        if outcome is None:
            return

        node_result = getattr(node, "result", None) or (outcome.result if hasattr(outcome, "result") else None)
        outcome_status = str(getattr(outcome, "status", "") or "failed")
        completed = outcome_status == "completed"
        tool_status = "success" if completed else ("pending_approval" if outcome_status == "waiting_approval" else "failed")
        if not tool_name:
            tool_name = _node_tool_from_result(node_result) or _node_tool_hint(node)
            if tool_name:
                yield {
                    "type": EVENT_TOOL_STARTED,
                    "job_id": job_id,
                    "step_id": step_id,
                    "call_id": call_id,
                    "tool": tool_name,
                    "display": str(step.get("title") or "")[:200],
                }
        if tool_name:
            summary = _result_summary(node_result)
            yield {
                "type": EVENT_TOOL_COMPLETED,
                "job_id": job_id,
                "step_id": step_id,
                "call_id": call_id,
                "tool": tool_name,
                "status": tool_status,
                "summary": summary,
                "error_code": (getattr(outcome, "error_code", "") or "") if not completed else None,
            }

        job = await self._store.get_job(job_id) or job
        async for event in self._settle_step(
            job=job,
            node=node,
            step_id=step_id,
            index=index,
            outcome_status=outcome_status,
            result_summary=_result_summary(node_result, getattr(node, "error", "") or ""),
            error=(getattr(outcome, "error", "") or getattr(node, "error", "") or None),
            error_code=(getattr(outcome, "error_code", "") or getattr(node, "error_code", "") or ""),
            call_id=call_id,
        ):
            yield event

    # ── 节点执行（实时 process 事件流）─────────────────────────

    async def _execution_event_stream(
        self,
        *,
        job: Job,
        node: TaskNode,
        step_id: str,
        job_id: str,
        sequence,
        holder: dict[str, Any],
    ) -> AsyncGenerator[dict, None]:
        """执行单节点并把节点文本输出以 process 事件实时发出。"""
        try:
            from app.agents.orchestration.execution.lifecycle import ApplicationNodeLifecycle
            from app.agents.orchestration.execution.node import ApplicationTaskNodeExecutor
            from app.agents.orchestration.job_contract import freeze_job_spec
        except Exception as exc:  # noqa: BLE001
            logger.error("单步执行依赖装配失败 {}: {}", job.job_id, exc)
            yield _error_event("单步执行器装配失败，请稍后重试", code="STEP_RUNTIME_ERROR", status=500)
            holder["outcome"] = None
            return

        spec = freeze_job_spec(job)
        spec_node = next((n for n in spec.nodes if n.id == step_id), None)
        if spec_node is None:
            yield _error_event("当前步骤未出现在冻结任务规格中", code="STEP_NOT_FOUND", status=500)
            holder["outcome"] = None
            return

        llm_config = self._llm_configs.get(job.job_id)
        executor = ApplicationTaskNodeExecutor(
            job=job,
            workers=self._workers,
            review=self._review,
            store=self._store,
            llm_api_key=None,
            llm_config=llm_config,
        )
        lifecycle = ApplicationNodeLifecycle(job, self._store)

        async def run() -> Any:
            result = await executor.execute_node(spec, spec_node, {})
            await lifecycle.on_node_state(spec, spec_node, None, result)
            return result

        task = asyncio.create_task(run())
        cursor = 0

        def emit_deltas(deltas: list[dict]) -> list[dict]:
            events: list[dict] = []
            for delta in deltas:
                text = str(delta.get("content") or "").strip()
                if not text:
                    continue
                events.append({
                    "type": "process",
                    "job_id": job_id,
                    "step_id": step_id,
                    "content": text,
                    "sequence": next(sequence),
                })
            return events

        while not task.done():
            try:
                from app.services.office_stream import read_deltas

                deltas, cursor = await read_deltas(job.job_id, cursor)
            except Exception:  # noqa: BLE001
                deltas, cursor = [], cursor
            for event in emit_deltas(deltas):
                yield event
            try:
                done, _ = await asyncio.wait({task}, timeout=self._poll_interval)
                if done:
                    break
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        # 收尾：补发任务结束前后尚未读取的增量。
        try:
            from app.services.office_stream import read_deltas

            tail, _ = await read_deltas(job.job_id, cursor)
        except Exception:  # noqa: BLE001
            tail = []
        for event in emit_deltas(tail):
            yield event
        try:
            outcome = task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("单步节点执行异常 {} | {}: {}", job.job_id, step_id, str(exc)[:240])
            outcome = type("Outcome", (), {
                "status": "failed",
                "error": str(exc) or "单步节点执行异常",
                "error_code": "STEP_EXECUTION_ERROR",
                "result": None,
            })()
        holder["outcome"] = outcome

    # ── 步骤结果回写与 canonical 推进 ─────────────────────────

    async def _settle_step(
        self,
        *,
        job: Job,
        node: TaskNode,
        step_id: str,
        index: int,
        outcome_status: str,
        result_summary: str,
        error: str | None,
        error_code: str,
        call_id: str,
    ) -> AsyncGenerator[dict, None]:
        fresh = await self._store.get_job(job.job_id)
        if fresh is not None:
            job = fresh
        live_node = next((n for n in job.nodes if n.id == step_id), node)
        plan_revision = int((job.routing or {}).get("plan_revision") or 1)

        if outcome_status == "waiting_approval" or job.status == JobStatus.WAITING_APPROVAL:
            await self._handle_escalation(job)
            job = await self._store.get_job(job.job_id) or job
            _mark_step_status(job, index, status="waiting_approval")
            job.routing = dict(job.routing or {})
            job.routing["execution_state"] = "waiting_approval"
            job.updated_at = time.time()
            await self._store.save_job(job)
            await self._suspend_capacity(job)
            view = run_view(job)
            risk = str(
                (live_node.approval_note if hasattr(live_node, "approval_note") else "")
                or "高危操作需要你的确认"
            )[:200]
            yield {
                "type": EVENT_WAITING_APPROVAL,
                "job_id": job.job_id,
                "step_id": step_id,
                "call_id": call_id,
                "status": "waiting_approval",
                "summary": str(result_summary or error or "")[:200],
                "risk": risk,
                "run_view": view,
            }
            yield self._done_event(job.job_id, view)
            return

        if outcome_status == "waiting_resources":
            await self._suspend_capacity(job)
            await self._revert_to_waiting(job, index=index, idempotency_key="")
            view = run_view(job)
            yield _error_event(
                "写资源协调服务暂不可用，请稍后重试该步骤",
                code="RESOURCE_COORDINATION_UNAVAILABLE",
            )
            yield self._done_event(job.job_id, view)
            return

        if outcome_status == "failed" or live_node.status in {
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
            TaskStatus.ESCALATED,
        }:
            async for event in self._settle_failure(
                job, live_node, index, step_id, plan_revision, result_summary, error, error_code,
            ):
                yield event
            return

        async for event in self._settle_success(
            job, live_node, index, step_id, plan_revision, result_summary,
        ):
            yield event

    async def _settle_success(
        self, job: Job, node: TaskNode, index: int, step_id: str,
        plan_revision: int, result_summary: str,
    ) -> AsyncGenerator[dict, None]:
        result_ref = await _persist_node_result_ref(job.user_id, node.result or {})
        _update_step_fields(
            job, index, status="completed", result_ref=result_ref,
            result_summary=result_summary,
        )
        steps = [dict(s) for s in (job.routing or {}).get("steps") or [] if isinstance(s, dict)]
        remaining = [
            s for s in steps
            if str(s.get("status") or "pending") in {"pending", "waiting_approval"}
        ]
        next_index = index + 1
        while next_index < len(steps) and str(steps[next_index].get("status") or "pending") not in {
            "pending", "waiting_approval",
        }:
            next_index += 1

        step_completed = {
            "type": EVENT_STEP_COMPLETED,
            "job_id": job.job_id,
            "step_id": step_id,
            "step_index": int(index),
            "status": "completed",
            "result_summary": str(result_summary or "")[:400],
            "plan_revision": plan_revision,
        }
        if remaining:
            job.routing = dict(job.routing or {})
            job.routing["current_step_index"] = next_index if next_index < len(steps) else index + 1
            job.routing["execution_state"] = "waiting_next"
            job.status = JobStatus.PENDING
            job.updated_at = time.time()
            await self._store.save_job(job)
            await self._suspend_capacity(job)
            view = run_view(job)
            next_step_id = ""
            if next_index < len(steps):
                next_step_id = str(steps[next_index].get("id") or "")
            yield step_completed
            yield {
                "type": EVENT_WAITING_NEXT,
                **waiting_next_payload(
                    job_id=job.job_id,
                    view=view,
                    completed_step_id=step_id,
                    next_step_id=next_step_id,
                ),
            }
            yield self._done_event(job.job_id, view)
            return

        # 最后一步完成 → 整个计划收敛为 completed。
        job.routing = dict(job.routing or {})
        job.routing["current_step_index"] = len(steps)
        job.routing["execution_state"] = "completed"
        job.status = JobStatus.COMPLETED
        job.updated_at = time.time()
        await self._store.save_job(job)
        if self._finalize_completed is not None:
            await self._finalize_completed(job)
            job = await self._store.get_job(job.job_id) or job
        view = run_view(job)
        yield step_completed
        yield {
            "type": EVENT_TASK_COMPLETED,
            "job_id": job.job_id,
            "status": "completed",
            "final_answer": str(view["final_answer"] or self._final_answer(job))[:20000],
            "run_view": view,
        }
        yield self._done_event(job.job_id, view)

    async def _settle_failure(
        self, job: Job, node: TaskNode, index: int, step_id: str,
        plan_revision: int, result_summary: str, error: str | None, error_code: str,
    ) -> AsyncGenerator[dict, None]:
        node_error = str(error or node.error or "该步骤执行失败")[:2000]
        node_code = str(error_code or node.error_code or "STEP_FAILED")[:80]
        _update_step_fields(
            job, index, status="failed",
            result_summary=node_error[:400], error=node_error,
        )
        routing = dict(job.routing or {})
        routing["execution_state"] = "failed"
        job.routing = routing
        job.status = JobStatus.FAILED
        job.error = node_error
        job.updated_at = time.time()
        await self._store.save_job(job)
        if self._finalize_failed is not None:
            await self._finalize_failed(job)
            job = await self._store.get_job(job.job_id) or job
        view = run_view(job)
        yield {
            "type": EVENT_STEP_COMPLETED,
            "job_id": job.job_id,
            "step_id": step_id,
            "step_index": int(index),
            "status": "failed",
            "result_summary": node_error[:400],
            "plan_revision": plan_revision,
        }
        yield {
            "type": EVENT_TASK_FAILED,
            "job_id": job.job_id,
            "step_id": step_id,
            "status": "failed",
            "error": node_error,
            "error_code": node_code,
            "retryable": False,
            "run_view": view,
        }
        yield self._done_event(job.job_id, view)

    async def _revert_to_waiting(self, job: Job, *, index: int, idempotency_key: str) -> None:
        """容量不足/资源等待时把 canonical 回滚到 waiting_run/waiting_next。"""
        try:
            job.routing = dict(job.routing or {})
            seen = list(job.routing.get("seen_step_keys") or [])
            if idempotency_key and seen and seen[-1] == idempotency_key:
                seen.pop()
                job.routing["seen_step_keys"] = seen
            state = "waiting_run" if index <= 0 else "waiting_next"
            job.routing["execution_state"] = state
            job.status = JobStatus.PENDING
            _mark_step_status(job, index, status="pending")
            job.updated_at = time.time()
            await self._store.save_job(job)
            await self._suspend_capacity(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("单步回滚到等待状态失败 {}: {}", job.job_id, str(exc)[:200])

    # ── 终态事件 ──────────────────────────────────────────────

    @staticmethod
    def _done_event(job_id: str, view: dict) -> dict:
        """终态校准事件：必须携带完整 run_view（前端契约）。"""
        return {
            "type": EVENT_DONE,
            "job_id": str(job_id),
            "status": view["status"],
            "run_view": view,
        }

    def _task_terminal_event(self, job: Job, view: dict) -> dict:
        if view["status"] == "completed":
            return {
                "type": EVENT_TASK_COMPLETED,
                "job_id": job.job_id,
                "status": "completed",
                "final_answer": str(view["final_answer"] or self._final_answer(job))[:20000],
                "run_view": view,
            }
        return {
            "type": EVENT_TASK_FAILED,
            "job_id": job.job_id,
            "status": view["status"],
            "error": str(job.error or "")[:2000],
            "error_code": str((job.result or {}).get("error_code") or "")[:80],
            "retryable": False,
            "run_view": view,
        }

    @staticmethod
    def _final_answer(job: Job) -> str:
        result = job.result if isinstance(job.result, dict) else {}
        answer = str(result.get("final_answer") or result.get("answer") or "").strip()
        if answer:
            return answer[:20000]
        blocks = []
        for node in job.nodes:
            if node.status != TaskStatus.COMPLETED:
                continue
            value = node.result or {}
            content = str(value.get("content") or value.get("output") or "").strip()
            if content:
                blocks.append(content)
        return "\n\n".join(blocks)[:20000]

    # ── 前置校验 ──────────────────────────────────────────────

    def _validate(
        self,
        job: Job,
        *,
        current_step_id: str,
        expected_step_id: str,
        requested_plan_revision: int | None,
        idempotency_key: str,
        workspace_bound: bool,
        dependencies_done: bool,
    ) -> dict:
        routing = job.routing if isinstance(job.routing, dict) else {}
        state = str(routing.get("execution_state") or "").strip()
        canonical = state if state else canonical_state_for(job)
        seen = tuple(str(x) for x in routing.get("seen_step_keys") or [])
        result = validate_resume_request(ResumeCheckInput(
            user_id=job.user_id,
            job_owner=job.user_id,
            job_state=canonical,
            current_step_id=current_step_id,
            expected_step_id=expected_step_id,
            plan_revision=int(requested_plan_revision or routing.get("plan_revision") or 1),
            current_revision=int(routing.get("plan_revision") or 1),
            idempotency_key=idempotency_key,
            seen_keys=seen,
            workspace_bound=bool(workspace_bound),
            dependencies_done=bool(dependencies_done),
        ))
        if result.allowed:
            return {"ok": True}
        return {"ok": False, "code": result.error_code or "JOB_NOT_RESUMABLE", "reason": result.reason}


async def _persist_node_result_ref(user_id: str, result: dict | None) -> dict[str, str] | None:
    """把单步完成的 sanitized 结果写入结果库并返回引用（失败静默降级为 None）。"""
    try:
        from app.agents.orchestration.execution.lineage import persist_result_ref

        return await persist_result_ref(user_id, result)
    except Exception as exc:  # noqa: BLE001 - 引用写入失败不影响步骤状态回写
        logger.warning("步骤结果引用持久化失败（降级继续）: {}", str(exc)[:160])
        return None


__all__ = [
    "StepRunService",
    "locate_current_step",
]
