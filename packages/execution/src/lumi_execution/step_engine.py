"""计划式任务“单步执行”驱动（执行内核，ports 由宿主提供）。

职责边界：
  - 本模块拥有：步骤定位、单步校验接入、幂等登记、canonical 状态迁移、
    步骤状态写回、执行过程事件（process/delta 队列）、单任务互斥；
  - 宿主通过 ports 提供：状态读写、准入获取/释放、单节点执行、审批门处理、
    终态收尾；视图/载荷由注入的 builder 生成（执行包不依赖编排包）。

依赖方向：本模块只依赖标准库与本包的 step_contract/step_resume；视图与
载荷由宿主注入的 builder 生成，执行包不依赖编排包。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable, Protocol

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
    StepCandidate,
    StepOutcome,
    StepRunState,
    apply_step_fields,
    locate_next_step,
    mark_running,
    revert_to_waiting,
    settle_approval,
    settle_failure,
    settle_success,
)
from lumi_execution.step_resume import ResumeCheckInput, validate_resume_request
from lumi_contracts.events.process import (
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    derive_kind,
    sanitize_process_text,
)

# ── 过程条目语义字段（kind 由后端判定，前端只渲染）────────────────────
# 去重键与持久化投影（app.contracts.process_log）保持一致：
#   step:<step_id> / call:<call_id> / process:<step_id>:<sequence>
# 因此实时帧与刷新后的 run_view.process_log 合并成同一行。
#
# 执行内核不依赖 ``app``（packages/orchestration/tests/test_kernel_boundaries.py
# 强制），拿不到 presentation.py 的润色文案：这里只用**步骤自己声明的**标题/说明
# 生成安全摘要，绝不读模型推理、工具参数或工具原始响应。
PROCESS_ENTRY_TITLE = "执行过程"
STEP_ENTRY_TITLE = "执行步骤"


def _step_title(candidate: StepCandidate) -> str:
    """步骤标题 → 安全标题（空则返回空串，由调用方给兜底文案）。"""
    return sanitize_process_text(candidate.step.get("title") or "", limit=TITLE_MAX_CHARS)


def _step_intent(candidate: StepCandidate) -> str:
    """步骤声明的公开意图（说明优先、其次标题），一律过安全摘要。"""
    declared = candidate.step.get("description") or candidate.step.get("title") or ""
    return sanitize_process_text(declared, limit=SUMMARY_MAX_CHARS)


def _step_kind(candidate: StepCandidate) -> str:
    """按步骤声明的工具判定 kind；未声明工具时是 thinking（与持久化一致）。"""
    tool_name = str(candidate.step.get("tool") or "").strip()
    if tool_name:
        return str(derive_kind(tool_name=tool_name))
    return str(derive_kind(event_type="step_started"))


class StepRunPorts(Protocol):
    """宿主适配面（不得包含编排决策）。"""

    async def load_state(self, job_id: str) -> StepRunState | None: ...

    async def save_state(self, state: StepRunState) -> None: ...

    async def acquire_capacity(self, state: StepRunState) -> bool: ...

    async def release_capacity(self, state: StepRunState) -> None: ...

    async def execute_step(
        self,
        state: StepRunState,
        step_id: str,
        on_process: Callable[[str], Awaitable[None]],
    ) -> StepOutcome: ...

    async def handle_escalation(self, state: StepRunState) -> None: ...

    async def finalize_completed(self, state: StepRunState) -> None: ...

    async def finalize_failed(self, state: StepRunState) -> None: ...


class StepRunEngine:
    """单步执行驱动：把 ports 串成一次 run_next 流程并产出契约事件。

    ``view_builder`` / ``waiting_next_builder`` 由宿主注入（通常复用编排层的
    run_view 投影），使执行内核不反向依赖编排包。
    """

    def __init__(
        self,
        *,
        ports: StepRunPorts,
        view_builder: Callable[[StepRunState], dict],
        waiting_next_builder: Callable[..., dict] | None = None,
        poll_interval: float = 0.12,
    ) -> None:
        self._ports = ports
        self._view = view_builder
        self._waiting_next = waiting_next_builder or self._default_waiting_next
        self._poll_interval = max(0.02, float(poll_interval))
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _default_waiting_next(
        *, state: StepRunState, view: dict, completed_step_id: str, next_step_id: str,
    ) -> dict:
        index = -1
        for step in state.steps:
            if str(step.get("id") or "") == str(next_step_id or ""):
                index = int(step.get("index", -1)) if "index" in step else state.steps.index(step)
                break
        return {
            "job_id": state.job_id,
            "status": "waiting_next",
            "completed_step_id": completed_step_id,
            "next_step_id": next_step_id,
            "next_step_index": index,
            "plan_revision": int(state.plan_revision or 1),
            "run_view": view,
        }

    async def run_next_stream(
        self,
        *,
        job_id: str,
        expected_step_id: str = "",
        idempotency_key: str = "",
        workspace_bound: bool = True,
        plan_revision: int | None = None,
    ) -> AsyncIterator[dict]:
        lock = self._locks.setdefault(job_id, asyncio.Lock())
        if lock.locked():
            yield self._error_event("该任务正在执行上一步，请稍候再试", "STEP_ALREADY_RUNNING", 409)
            return
        async with lock:
            async for event in self._run_locked(
                job_id=job_id,
                expected_step_id=expected_step_id,
                idempotency_key=idempotency_key,
                workspace_bound=workspace_bound,
                plan_revision=plan_revision,
            ):
                yield event

    async def _run_locked(
        self,
        *,
        job_id: str,
        expected_step_id: str,
        idempotency_key: str,
        workspace_bound: bool,
        plan_revision: int | None = None,
    ) -> AsyncIterator[dict]:
        state = await self._ports.load_state(job_id)
        if state is None:
            yield self._error_event("任务不存在或状态已过期", "JOB_NOT_FOUND", 404)
            return
        candidate = locate_next_step(state)
        if candidate is None:
            view = self._view(state)
            if state.canonical in {"completed", "failed", "cancelled"}:
                yield self._terminal_event(state, view)
            else:
                yield self._error_event("当前没有可执行的下一步（计划已无待运行步骤）", "STEP_NOT_FOUND")
            yield self._done_event(job_id, view)
            return

        effective_key = str(idempotency_key or "").strip() or f"run-next-{uuid.uuid4().hex}"
        check = self._validate(
            state,
            current_step_id=candidate.step_id,
            expected_step_id=expected_step_id,
            idempotency_key=effective_key,
            workspace_bound=workspace_bound,
            dependencies_done=bool(candidate.step.get("dependencies_done", True)),
            plan_revision=plan_revision,
        )
        if not check["ok"]:
            yield self._error_event(check["reason"], check["code"])
            yield self._done_event(job_id, self._view(state))
            return

        seen = list(state.seen_keys)
        seen.append(effective_key)
        state.seen_keys = seen[-50:]
        mark_running(state, candidate)
        state.updated_at = time.time()
        await self._ports.save_state(state)

        state = await self._ports.load_state(job_id) or state
        if not await self._ports.acquire_capacity(state):
            revert_to_waiting(state, candidate.index, effective_key)
            await self._ports.save_state(state)
            await self._ports.release_capacity(state)
            yield self._error_event("办公执行容量已满，请稍后重试“运行下一步”", "STEP_CAPACITY_UNAVAILABLE", 429)
            yield self._done_event(job_id, self._view(state))
            return

        yielded_revision = int(state.plan_revision or 1)
        intent = _step_intent(candidate)
        yield {
            "type": SSE_EVENT_STEP_STARTED,
            "job_id": job_id,
            "step_id": candidate.step_id,
            "step_index": candidate.index,
            "title": str(candidate.step.get("title") or "")[:200],
            "plan_revision": yielded_revision,
            # 语义过程字段：entry_id 与持久化 step:<id> 对齐（刷新后同一行）；
            # title/status 由统一字段覆盖为净化后的安全值。
            **self._step_semantics(
                candidate, status="running", summary=intent or "正在执行该步骤",
            ),
        }

        queue: asyncio.Queue[str] = asyncio.Queue()
        tool_name = str(candidate.step.get("tool") or "")[:80]
        call_id = f"call-{candidate.step_id}"

        async def on_process(text: str) -> None:
            await queue.put(str(text or ""))

        task = asyncio.create_task(self._ports.execute_step(state, candidate.step_id, on_process))
        sequence = 0
        process_summary = f"正在处理：{intent}" if intent else "正在处理当前步骤"
        if tool_name:
            yield self._tool_started(job_id, candidate, tool_name, call_id)
        while not task.done() or not queue.empty():
            try:
                text = await asyncio.wait_for(queue.get(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue
            if text:
                sequence += 1
                yield {
                    "type": "process", "job_id": job_id, "step_id": candidate.step_id,
                    "content": text, "sequence": sequence,
                    # 语义过程字段：summary 只用步骤声明的公开意图做进度短语，
                    # **绝不**复制模型正文到摘要（过程不是推理链）；content 原样保留。
                    "entry_id": f"process:{candidate.step_id}:{sequence}",
                    "kind": str(derive_kind(event_type="process")),
                    "title": _step_title(candidate) or PROCESS_ENTRY_TITLE,
                    "summary": process_summary,
                    "status": "running",
                }
        outcome: StepOutcome = task.result()
        state = await self._ports.load_state(job_id) or state

        if not tool_name:
            tool_name = str(outcome.tool_name or "")
            if tool_name:
                yield self._tool_started(job_id, candidate, tool_name, call_id)
        if tool_name:
            completed = outcome.status == "completed"
            pending_approval = outcome.status == "waiting_approval"
            # status 是**过程状态**（completed/running/failed，与统一过程契约一致）；
            # 旧的工具级状态（success/pending_approval/failed）保留在 tool_status。
            tool_summary = sanitize_process_text(outcome.result_summary or "", limit=SUMMARY_MAX_CHARS)
            if not tool_summary:
                tool_summary = (
                    f"已完成 {tool_name}" if completed
                    else (f"等待确认后继续 {tool_name}" if pending_approval else f"{tool_name} 未完成")
                )
            yield {
                "type": SSE_EVENT_TOOL_COMPLETED,
                "job_id": job_id,
                "step_id": candidate.step_id,
                "call_id": call_id,
                "tool": tool_name,
                "status": "completed" if completed else ("running" if pending_approval else "failed"),
                "tool_status": (
                    "success" if completed else ("pending_approval" if pending_approval else "failed")
                ),
                "summary": tool_summary,
                "error_code": None if completed else (outcome.error_code or None),
                # 语义过程字段：entry_id 用稳定 call_id，与 tool_started 同一行。
                "entry_id": f"call:{call_id}",
                "kind": str(derive_kind(tool_name=tool_name)),
                "title": _step_title(candidate) or STEP_ENTRY_TITLE,
            }

        async for event in self._settle(state, candidate, outcome, yielded_revision):
            yield event

    async def _settle(
        self,
        state: StepRunState,
        candidate: StepCandidate,
        outcome: StepOutcome,
        plan_revision: int,
    ) -> AsyncIterator[dict]:
        if outcome.status == "waiting_approval" or state.job_status == "waiting_approval":
            await self._ports.handle_escalation(state)
            state = await self._ports.load_state(state.job_id) or state
            settle_approval(state, candidate)
            state.updated_at = time.time()
            await self._ports.save_state(state)
            await self._ports.release_capacity(state)
            view = self._view(state)
            approval_call_id = f"call-{candidate.step_id}"
            approval_summary = sanitize_process_text(
                outcome.result_summary or "", limit=SUMMARY_MAX_CHARS
            )
            yield {
                "type": SSE_EVENT_WAITING_APPROVAL,
                "job_id": state.job_id,
                "step_id": candidate.step_id,
                "call_id": approval_call_id,
                # 该事件不在 SseEventEncoder 的统一过程字段集合里，字段原样上线：
                # status 保持既有审批语义（前端按它显示审批态），过程状态由 type 表达。
                "status": "waiting_approval",
                "summary": approval_summary or "等待你确认后继续",
                "risk": str(candidate.step.get("risk") or "高危操作需要你的确认")[:200],
                "run_view": view,
                # 语义过程字段：与 tool_* 共用稳定 call_id，等待/完成合并成同一行。
                "entry_id": f"call:{approval_call_id}",
                "kind": _step_kind(candidate),
                "title": _step_title(candidate) or STEP_ENTRY_TITLE,
            }
            yield self._done_event(state.job_id, view)
            return

        if outcome.status == "waiting_resources":
            await self._ports.release_capacity(state)
            revert_to_waiting(state, candidate.index)
            await self._ports.save_state(state)
            yield self._error_event("写资源协调服务暂不可用，请稍后重试该步骤", "RESOURCE_COORDINATION_UNAVAILABLE")
            yield self._done_event(state.job_id, self._view(state))
            return

        if outcome.status == "failed":
            settle_failure(state, candidate, outcome.error, outcome.error_code)
            state.updated_at = time.time()
            await self._ports.save_state(state)
            await self._ports.finalize_failed(state)
            view = self._view(state)
            yield {
                "type": SSE_EVENT_STEP_COMPLETED,
                "job_id": state.job_id,
                "step_id": candidate.step_id,
                "step_index": candidate.index,
                "status": "failed",
                # result_summary 是**步骤级**字段（不是最终答复），保持字段名不变；
                # 值一律过安全摘要（绝对路径/凭据/协议痕迹不落）。
                "result_summary": sanitize_process_text(
                    outcome.error or "", limit=SUMMARY_MAX_CHARS
                ),
                "plan_revision": plan_revision,
                **self._step_semantics(
                    candidate,
                    status="failed",
                    summary=outcome.error or "该步骤未完成",
                ),
            }
            yield {
                "type": SSE_EVENT_TASK_FAILED,
                "job_id": state.job_id,
                "step_id": candidate.step_id,
                "status": "failed",
                "error": str(outcome.error or "")[:2000],
                "error_code": str(outcome.error_code or "STEP_FAILED")[:80],
                "retryable": False,
                "run_view": view,
            }
            yield self._done_event(state.job_id, view)
            return

        apply_step_fields(
            state, candidate.index, status="completed",
            result_ref=outcome.result_ref, result_summary=outcome.result_summary,
        )
        waiting = settle_success(state, candidate)
        state.updated_at = time.time()
        await self._ports.save_state(state)
        step_completed = {
            "type": SSE_EVENT_STEP_COMPLETED,
            "job_id": state.job_id,
            "step_id": candidate.step_id,
            "step_index": candidate.index,
            "status": "completed",
            # 步骤级结果摘要（明确不是最终答复：final_answer 只来自 run_view），
            # 值过安全摘要后再进过程字段。
            "result_summary": sanitize_process_text(
                outcome.result_summary or "", limit=SUMMARY_MAX_CHARS
            ),
            "plan_revision": plan_revision,
            **self._step_semantics(
                candidate,
                status="completed",
                summary=outcome.result_summary or "该步骤已完成",
            ),
        }
        if waiting:
            await self._ports.release_capacity(state)
            view = self._view(state)
            yield step_completed
            yield {
                "type": SSE_EVENT_WAITING_NEXT,
                **self._waiting_next(
                    state=state,
                    view=view,
                    completed_step_id=candidate.step_id,
                    next_step_id=str(waiting.get("next_step_id") or ""),
                ),
            }
            yield self._done_event(state.job_id, view)
            return

        await self._ports.finalize_completed(state)
        state = await self._ports.load_state(state.job_id) or state
        view = self._view(state)
        yield step_completed
        yield {
            "type": SSE_EVENT_TASK_COMPLETED,
            "job_id": state.job_id,
            "status": "completed",
            "final_answer": str(view.get("final_answer") or state.final_answer or "")[:20000],
            "run_view": view,
        }
        yield self._done_event(state.job_id, view)

    def _validate(
        self,
        state: StepRunState,
        *,
        current_step_id: str,
        expected_step_id: str,
        idempotency_key: str,
        workspace_bound: bool,
        dependencies_done: bool,
        plan_revision: int | None = None,
    ) -> dict:
        result = validate_resume_request(ResumeCheckInput(
            user_id=state.user_id,
            job_owner=state.user_id,
            job_state=state.canonical,
            current_step_id=current_step_id,
            expected_step_id=expected_step_id,
            plan_revision=int(plan_revision if plan_revision is not None else state.plan_revision or 1),
            current_revision=int(state.plan_revision or 1),
            idempotency_key=idempotency_key,
            seen_keys=tuple(state.seen_keys),
            workspace_bound=bool(workspace_bound),
            dependencies_done=bool(dependencies_done),
        ))
        if result.allowed:
            return {"ok": True}
        return {"ok": False, "code": result.error_code or "JOB_NOT_RESUMABLE", "reason": result.reason}

    @staticmethod
    def _step_semantics(candidate: StepCandidate, *, status: str, summary: str) -> dict:
        """步骤事件的统一语义过程字段（entry_id 与持久化 step:<id> 对齐）。"""
        return {
            "entry_id": f"step:{candidate.step_id}",
            "kind": _step_kind(candidate),
            "title": _step_title(candidate) or STEP_ENTRY_TITLE,
            "summary": sanitize_process_text(summary, limit=SUMMARY_MAX_CHARS)
            or "正在执行该步骤",
            "status": status,
        }

    @staticmethod
    def _tool_started(job_id: str, candidate: StepCandidate, tool_name: str, call_id: str) -> dict:
        return {
            "type": SSE_EVENT_TOOL_STARTED,
            "job_id": job_id,
            "step_id": candidate.step_id,
            "call_id": call_id,
            "tool": tool_name,
            "display": str(candidate.step.get("title") or "")[:200],
            # 语义过程字段：entry_id 用稳定 call_id，与 tool_completed 同一行；
            # kind 由工具名判定（后端唯一判定处）。
            "entry_id": f"call:{call_id}",
            "kind": str(derive_kind(tool_name=tool_name)),
            "title": _step_title(candidate) or STEP_ENTRY_TITLE,
            "summary": sanitize_process_text(
                f"正在调用 {tool_name}", limit=SUMMARY_MAX_CHARS
            ),
            "status": "running",
        }

    @staticmethod
    def _error_event(message: str, code: str, status: int = 400) -> dict:
        return {"type": "error", "message": message, "status": status, "code": code}

    @staticmethod
    def _done_event(job_id: str, view: dict) -> dict:
        return {"type": SSE_EVENT_DONE, "job_id": job_id, "status": view["status"], "run_view": view}

    @staticmethod
    def _terminal_event(state: StepRunState, view: dict) -> dict:
        if view["status"] == "completed":
            return {
                "type": SSE_EVENT_TASK_COMPLETED,
                "job_id": state.job_id,
                "status": "completed",
                "final_answer": str(view.get("final_answer") or "")[:20000],
                "run_view": view,
            }
        return {
            "type": SSE_EVENT_TASK_FAILED,
            "job_id": state.job_id,
            "status": view["status"],
            "error": str(state.error or "")[:2000],
            "retryable": False,
            "run_view": view,
        }


__all__ = ["StepRunEngine", "StepRunPorts"]
