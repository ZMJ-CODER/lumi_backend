"""Step 运行服务：对外流式入口（**门面 + 执行流程**）。

本包按职责分子模块：

* 状态适配 → `step/state_adapter.py`；
* 过程日志与事件投影（与刷新快照同源）→ `step/presentation.py`；
* checkpoint 与结果引用持久化 → `step/persistence.py`。

本模块保留：SSE 事件常量、``StepRunService``（对外入口）与执行流程。
**不搬去 `lumi_execution`**：这里仍然带着应用层的 Job、事件与持久化依赖。
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
)
from lumi_execution.step_engine import StepRunEngine
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.step.persistence import (
    _persist_node_result_ref,
    _record_step_checkpoints,
)
from app.agents.orchestration.step.presentation import (
    _live_presentation_fields,
    _result_summary,
)
from app.agents.orchestration.step.state_adapter import (
    _state_from_job,
    locate_current_step,
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

_VALID_JOB_STATUS = {status.value for status in JobStatus}


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
        """转发内核事件流，并在 app 层把步骤文案换成"刷新后同一句"。

        内核（``lumi_execution.step_engine``）只按步骤声明的 instruction/title 发
        文案，且不得 import ``app.*``；刷新投影（``app/contracts/process_log.py``）
        用的是 ``presentation.intent_text/working_text/completed_text/failed_text``。
        这里按 ``status`` 覆盖实时帧的 ``title``/``summary``，让同一 ``entry_id``
        （``step:<id>``）的行在实时与刷新后逐字一致；其余字段一律不动。
        """
        return self._run_next_with_presentation(
            job_id=job_id,
            expected_step_id=expected_step_id,
            idempotency_key=idempotency_key,
            workspace_bound=workspace_bound,
            plan_revision=plan_revision,
        )

    async def _run_next_with_presentation(
        self,
        *,
        job_id: str,
        expected_step_id: str,
        idempotency_key: str,
        workspace_bound: bool,
        plan_revision: int | None,
    ):
        job: Job | None = None
        try:
            job = await self._store.get_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 文案注入失败不能中断执行
            logger.warning("过程文案注入读取任务失败 {}: {}", str(job_id)[:12], str(exc)[:160])
        # 能力状态帧：run_next 流里也要能看到"等待 Provider / 能力完成/失败"，
        # 否则客户端离线时用户只看到步骤一直转圈。事件流不可用时静默跳过。
        capability_cursor = 0
        async for event in self._engine.run_next_stream(
            job_id=job_id,
            expected_step_id=expected_step_id,
            idempotency_key=idempotency_key,
            workspace_bound=workspace_bound,
            plan_revision=plan_revision,
        ):
            step_id = str(event.get("step_id") or "") if isinstance(event, dict) else ""
            # 能力状态事件先于当帧输出（时间顺序：能力在跑 → 步骤状态跟上）。
            capability_cursor, pending = await self._collect_capability_events(
                job_id, capability_cursor, step_id=step_id
            )
            for capability_event in pending:
                yield capability_event
            fields = _live_presentation_fields(job, event) if isinstance(event, dict) else {}
            if fields:
                event = {**event, **fields}
            await self._confirm_checkpoint_before_emit(event)
            yield event

    @staticmethod
    async def _confirm_checkpoint_before_emit(event: dict[str, Any]) -> None:
        """方案 §2.3 铁律的落点：完成事件必须在检查点落盘之后。

        内核已经保证"先写 Job 状态、再发事件"（``save_state`` 在 ``_settle`` 的 yield
        之前），本函数在**应用层出口**再核对一次该步骤的检查点已落盘：核对通过才记
        完成事件，未落盘时打警告——前端仍有 Job 快照兜底，但运维侧能立刻看到"这件事
        不该发生"，而不是静默出一个"刷新后找不到结果"的完成态。

        注意：这里不"吞掉"事件。吞掉会让前端停在中途（比快照不一致更糟），而方案的
        铁律由**写入顺序**保证，这里只是把可能的顺序破坏变成可见告警。
        """
        from lumi_contracts.persistence.checkpoint import must_persist_before_emit

        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "")
        if not must_persist_before_emit(event_type):
            return
        step_id = str(event.get("step_id") or "")
        job_id = str(event.get("job_id") or "")
        if not step_id or not job_id:
            return
        try:
            from app.services.step_checkpoint import coordinator_for

            coordinator = coordinator_for(job_id)
            if not coordinator.enabled:
                return
            version = await coordinator.confirm_persisted_for_emit(step_id, event_type)
            if version <= 0:
                logger.warning(
                    "[checkpoint] 完成事件缺少已落盘检查点 job={} step={} type={}（快照仍可恢复，请排查写入顺序）",
                    job_id[:12],
                    step_id[:24],
                    event_type,
                )
        except Exception as exc:  # noqa: BLE001 - 核对失败不能影响事件投递
            logger.debug("[checkpoint] 完成事件核对失败: {}", str(exc)[:120])

    @staticmethod
    async def _collect_capability_events(
        job_id: str, cursor: int, *, step_id: str = ""
    ) -> tuple[int, list[dict]]:
        """按游标取出能力状态事件（返回新游标 + 事件列表）。"""
        try:
            from app.services.capability_events import read_capability_events

            events, cursor = await read_capability_events(job_id, cursor)
        except Exception:  # noqa: BLE001 - 状态流不可用不能中断执行
            return cursor, []
        rows: list[dict] = []
        for item in events:
            if step_id and not item.get("step_id"):
                # 补 step_id 便于前端把能力状态归到当前步骤行。
                item = {**item, "step_id": step_id}
            rows.append(item)
        return cursor, rows

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
        # 执行过程日志：由本次保存的最新步骤状态派生并**合并**（重复保存/重跑同一步
        # 不重复，列表有界 ≤200）。落 Job 快照本身，不放进 routing（routing 只存
        # 路由/策略），供刷新后 GET /agents/jobs/{id} 恢复过程气泡。
        from app.contracts.process_log import persist_process_log

        persist_process_log(job)
        # 阶段 3（ARCHIVE_CONTENT_V2，默认关闭）：窗口外的更早日志落成真实归档产物，
        # 并把可读的 log_archive_ref 记进 routing。关闭时是空操作（不写文件、不加字段）。
        from app.services.process_log_archive import archive_process_log_overflow

        archive_process_log_overflow(job)
        await self._store.save_job(job)
        # 方案 §2.3 第 6/7 步：结果引用与任务状态都落盘之后，才写步骤检查点。
        # 完成事件由内核对这一份已保存状态发射 → "完成事件晚于检查点"由顺序保证。
        await _record_step_checkpoints(job, state)

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
        from app.agents.orchestration.runtime.job_contract import freeze_job_spec

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
            # 方案 §1.3：引用携带**存储时**的 schema 版本，读取时按它解析历史数据。
            live_result = live.result if isinstance(live.result, dict) else {}
            result_ref = await _persist_node_result_ref(
                job.user_id,
                live.result or {},
                job_id=job.job_id,
                step_id=step_id,
                tool_name=str(live_result.get("tool") or ""),
                schema_name=str(live_result.get("schema_name") or "execution_result"),
                schema_version=int(live_result.get("schema_version") or 1),
            )
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
            from app.office.api import read_deltas

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
