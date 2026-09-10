"""单步执行（/jobs/{id}/resume action=run_next）应用适配层。

编排/迁移逻辑位于 ``lumi_orch.step_engine``（StepRunEngine + ports）。
本模块只做三件事：
  1) Job ↔ StepRunState 映射与 store 读写；
  2) 通过 ApplicationTaskNodeExecutor/Lifecycle 执行当前节点（node 执行仍复用
     app 侧执行适配器，底层是 lumi_execution 引擎）；
  3) 准入、审批门、终态收尾回调的接线。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from loguru import logger

from lumi_execution.step_contract import (
    SSE_EVENT_DONE,
    SSE_EVENT_STEP_COMPLETED,
    SSE_EVENT_STEP_STARTED,
    SSE_EVENT_TASK_COMPLETED,
    SSE_EVENT_TASK_FAILED,
    SSE_EVENT_TOOL_COMPLETED,
    SSE_EVENT_TOOL_STARTED,
    SSE_EVENT_WAITING_APPROVAL,
    SSE_EVENT_WAITING_NEXT,
    StepOutcome,
    StepRunState,
    locate_next_step,
)
from lumi_execution.step_engine import StepRunEngine
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
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

_VALID_JOB_STATUS = {status.value for status in JobStatus}


def locate_current_step(job: Job):
    """兼容入口：由 Job 定位下一步（实现委托给执行内核 step_engine）。"""
    state = _state_from_job(job)
    candidate = locate_next_step(state)
    if candidate is None:
        return None, -1, None, False
    node = next((n for n in job.nodes if n.id == candidate.step_id), None)
    return candidate.step, candidate.index, node, candidate.has_more


def _state_from_job(job: Job) -> StepRunState:
    routing = job.routing if isinstance(job.routing, dict) else {}
    steps: list[dict] = []
    nodes_by_id = {node.id: node for node in job.nodes}
    for raw in routing.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        step = dict(raw)
        node = nodes_by_id.get(str(step.get("id") or ""))
        if node is not None:
            step["dependencies_done"] = _dependencies_done(job, node)
            hint = str((node.params or {}).get("preferred_tool") or "")
            step.setdefault("tool", hint)
        else:
            step["dependencies_done"] = False
        steps.append(step)
    result = job.result if isinstance(job.result, dict) else {}
    return StepRunState(
        job_id=job.job_id,
        user_id=job.user_id,
        job_status=job.status.value if hasattr(job.status, "value") else str(job.status),
        canonical=str(routing.get("execution_state") or ""),
        plan_revision=int(routing.get("plan_revision") or 1),
        current_step_index=int(routing.get("current_step_index") or 0),
        steps=steps,
        seen_keys=[str(x) for x in routing.get("seen_step_keys") or []],
        error=job.error,
        updated_at=float(job.updated_at or 0.0),
        final_answer=str(result.get("final_answer") or result.get("answer") or ""),
        execution_mode=str(routing.get("execution_mode") or "step_confirm"),
        plan_text=str(routing.get("plan_text") or job.plan_text or ""),
    )


def _dependencies_done(job: Job, node: TaskNode) -> bool:
    if not node.depends_on:
        return True
    nodes_by_id = {item.id: item for item in job.nodes}
    return all(
        (dep := nodes_by_id.get(dep_id)) is not None and dep.status == TaskStatus.COMPLETED
        for dep_id in node.depends_on
    )


def _result_summary(result: dict | None, fallback: str = "") -> str:
    value = result or {}
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


async def _persist_node_result_ref(user_id: str, result: dict | None) -> dict[str, str] | None:
    try:
        from app.agents.orchestration.execution.lineage import persist_result_ref

        return await persist_result_ref(user_id, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("步骤结果引用持久化失败（降级继续）: {}", str(exc)[:160])
        return None


class StepRunService:
    """StepRunPorts 的 app 实现 + 对外流式入口。"""

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
        self._ensure_active_capacity = ensure_active_capacity or self._always_true
        self._suspend_capacity = suspend_capacity or (lambda _job: self._noop())
        self._handle_escalation = handle_escalation or (lambda _job: self._noop_false())
        self._finalize_completed = finalize_completed
        self._finalize_failed = finalize_failed
        # 视图/载荷由编排层投影注入，执行内核不反向依赖编排包。
        from lumi_orch.run_view import run_view, waiting_next_payload

        self._view_builder = lambda state: run_view(state.as_job_snapshot())
        self._waiting_next_builder = (
            lambda *, state, view, completed_step_id, next_step_id: waiting_next_payload(
                job_id=state.job_id,
                view=view,
                completed_step_id=completed_step_id,
                next_step_id=next_step_id,
            )
        )
        self._engine = StepRunEngine(
            ports=self,
            view_builder=self._view_builder,
            waiting_next_builder=self._waiting_next_builder,
            poll_interval=poll_interval,
        )

    @staticmethod
    async def _noop() -> None:
        return None

    @staticmethod
    async def _always_true(_job) -> bool:
        return True

    @staticmethod
    async def _noop_false(_job) -> bool:
        return False

    def run_next_stream(
        self,
        *,
        job_id: str,
        expected_step_id: str = "",
        idempotency_key: str = "",
        workspace_bound: bool = True,
        plan_revision: int | None = None,
    ):
        return self._engine.run_next_stream(
            job_id=job_id,
            expected_step_id=expected_step_id,
            idempotency_key=idempotency_key,
            workspace_bound=workspace_bound,
            plan_revision=plan_revision,
        )

    # ── StepRunPorts 实现 ─────────────────────────────────────

    async def load_state(self, job_id: str) -> StepRunState | None:
        job = await self._store.get_job(job_id)
        if job is None:
            return None
        return _state_from_job(job)

    async def save_state(self, state: StepRunState) -> None:
        job = await self._store.get_job(state.job_id)
        if job is None:
            return
        routing = dict(job.routing or {})
        routing["execution_state"] = state.canonical
        routing["execution_mode"] = state.execution_mode
        routing["plan_revision"] = int(state.plan_revision or 1)
        routing["current_step_index"] = int(state.current_step_index or 0)
        routing["steps"] = [dict(step) for step in state.steps]
        routing["seen_step_keys"] = list(state.seen_keys)[-50:]
        if state.plan_text:
            routing["plan_text"] = state.plan_text[:12000]
        job.routing = routing
        if state.job_status in _VALID_JOB_STATUS:
            job.status = JobStatus(state.job_status)
        job.error = state.error
        job.updated_at = state.updated_at or time.time()
        if state.final_answer:
            job.result = {**(job.result or {}), "final_answer": state.final_answer}
        await self._store.save_job(job)

    async def acquire_capacity(self, state: StepRunState) -> bool:
        job = await self._store.get_job(state.job_id)
        if job is None:
            return False
        return bool(await self._ensure_active_capacity(job))

    async def release_capacity(self, state: StepRunState) -> None:
        job = await self._store.get_job(state.job_id)
        if job is not None:
            await self._suspend_capacity(job)

    async def execute_step(
        self,
        state: StepRunState,
        step_id: str,
        on_process: Callable[[str], Awaitable[None]],
    ) -> StepOutcome:
        from app.agents.orchestration.execution.lifecycle import ApplicationNodeLifecycle
        from app.agents.orchestration.execution.node import ApplicationTaskNodeExecutor
        from app.agents.orchestration.job_contract import freeze_job_spec

        job = await self._store.get_job(state.job_id)
        if job is None:
            return StepOutcome(status="failed", error="任务状态已丢失", error_code="JOB_NOT_FOUND")
        node = next((n for n in job.nodes if n.id == step_id), None)
        if node is None:
            return StepOutcome(status="failed", error="当前步骤节点不存在", error_code="STEP_NOT_FOUND")

        spec = freeze_job_spec(job)
        spec_node = next((n for n in spec.nodes if n.id == step_id), None)
        if spec_node is None:
            return StepOutcome(status="failed", error="步骤未出现在冻结规格中", error_code="STEP_NOT_FOUND")

        executor = ApplicationTaskNodeExecutor(
            job=job,
            workers=self._workers,
            review=self._review,
            store=self._store,
            llm_api_key=None,
            llm_config=self._llm_configs.get(job.job_id),
        )
        lifecycle = ApplicationNodeLifecycle(job, self._store)

        async def run() -> Any:
            result = await executor.execute_node(spec, spec_node, {})
            await lifecycle.on_node_state(spec, spec_node, None, result)
            return result

        task = asyncio.create_task(run())
        cursor = 0
        while not task.done():
            cursor = await self._drain_process(job.job_id, node.id, cursor, on_process)
            try:
                done, _ = await asyncio.wait({task}, timeout=max(0.02, self._engine_poll()))
                if done:
                    break
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        await self._drain_process(job.job_id, node.id, cursor, on_process)
        try:
            outcome = task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("单步节点执行异常 {} | {}: {}", job.job_id, step_id, str(exc)[:240])
            return StepOutcome(status="failed", error=str(exc) or "单步节点执行异常",
                               error_code="STEP_EXECUTION_ERROR")

        live = next((n for n in job.nodes if n.id == step_id), node)
        status = str(getattr(outcome, "status", "failed") or "failed")
        result_ref = None
        if status == "completed":
            result_ref = await _persist_node_result_ref(job.user_id, live.result or {})
        return StepOutcome(
            status=status,
            result_summary=_result_summary(live.result, live.error or ""),
            error=str(getattr(outcome, "error", "") or live.error or ""),
            error_code=str(getattr(outcome, "error_code", "") or live.error_code or ""),
            result_ref=result_ref,
            tool_name=str((live.result or {}).get("tool") or ""),
        )

    def _engine_poll(self) -> float:
        return float(getattr(self._engine, "_poll_interval", 0.12))

    async def _drain_process(
        self,
        job_id: str,
        node_id: str,
        cursor: int,
        on_process: Callable[[str], Awaitable[None]],
    ) -> int:
        try:
            from app.services.office_stream import read_deltas

            deltas, cursor = await read_deltas(job_id, cursor)
        except Exception:  # noqa: BLE001
            return cursor
        for delta in deltas:
            if str(delta.get("node_id") or "") != node_id:
                continue
            text = str(delta.get("content") or "").strip()
            if text:
                await on_process(text)
        return cursor

    async def handle_escalation(self, state: StepRunState) -> None:
        job = await self._store.get_job(state.job_id)
        if job is not None:
            await self._handle_escalation(job)

    async def finalize_completed(self, state: StepRunState) -> None:
        if self._finalize_completed is None:
            return
        job = await self._store.get_job(state.job_id)
        if job is not None:
            await self._finalize_completed(job)

    async def finalize_failed(self, state: StepRunState) -> None:
        if self._finalize_failed is None:
            return
        job = await self._store.get_job(state.job_id)
        if job is not None:
            await self._finalize_failed(job)


__all__ = [
    "EVENT_DONE",
    "EVENT_STEP_COMPLETED",
    "EVENT_STEP_STARTED",
    "EVENT_TASK_COMPLETED",
    "EVENT_TASK_FAILED",
    "EVENT_TOOL_COMPLETED",
    "EVENT_TOOL_STARTED",
    "EVENT_WAITING_APPROVAL",
    "EVENT_WAITING_NEXT",
    "StepRunService",
    "locate_current_step",
]
