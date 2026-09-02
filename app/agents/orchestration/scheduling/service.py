"""运行期计划补图的调度服务。

补图先写入 Redis 逻辑计划，再唤醒对应运行时。Temporal Signal 只是通知，
不是计划内容的传输通道，因此 Signal 丢失或重复都不会损坏已持久化计划。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from lumi_orch import ExpansionSlot, NodeSpec, PlanPatch, PlanPatchConflict

from app.agents.orchestration.execution.validation import validate_planned_dag
from app.agents.orchestration.logical_plan import (
    load_logical_plan,
    logical_plan_progress,
    save_logical_plan,
)
from app.agents.orchestration.models import Job, JobStatus, TaskNode
from app.agents.orchestration.safety import prepare_node_safety
from app.agents.orchestration.scheduling.plan_patches import (
    apply_plan_patch,
    patch_fingerprint,
    ready_slots,
)
from app.agents.orchestration.scheduling.locking import PlanPatchLock


@dataclass(frozen=True, slots=True)
class PlanPatchAppendResult:
    """补图后的脱敏控制面结果。"""

    job: Job
    patch_id: str
    slot_id: str
    revision: int
    replayed: bool
    temporal_signaled: bool
    requires_legacy_resume: bool


class PlanPatchScheduler:
    """在执行层前验证、持久化并唤醒受限的运行期补图。"""

    def __init__(self, *, store: Any, workers: dict[str, Any]) -> None:
        self._store = store
        self._workers = workers
        self._locks: dict[str, PlanPatchLock] = {}

    async def append_external(
        self,
        *,
        job_id: str,
        user_id: str,
        patch: PlanPatch,
    ) -> PlanPatchAppendResult:
        """追加外部系统提供的补丁；外部来源不可伪装为 LangGraph。"""
        if patch.source != "external":
            raise PlanPatchConflict("外部接口仅接受 source=external 的补图")
        return await self._append(job_id=job_id, user_id=user_id, patch=patch)

    async def append_langgraph(
        self,
        *,
        job_id: str,
        user_id: str,
        patch: PlanPatch,
    ) -> PlanPatchAppendResult:
        """供内部 LangGraph 适配器使用；仍复用相同的持久化和安全校验。"""
        if patch.source != "langgraph":
            raise PlanPatchConflict("内部补图必须声明 source=langgraph")
        return await self._append(job_id=job_id, user_id=user_id, patch=patch)

    async def _append(
        self,
        *,
        job_id: str,
        user_id: str,
        patch: PlanPatch,
    ) -> PlanPatchAppendResult:
        lock = self._locks.setdefault(job_id, PlanPatchLock(job_id))
        async with lock:
            job = await self._store.get_job(job_id)
            if job is None or job.user_id != user_id:
                raise PlanPatchConflict("任务不存在或不属于当前用户")
            pointer = (job.routing or {}).get("logical_plan")
            if not isinstance(pointer, dict) or not pointer.get("plan_id"):
                raise PlanPatchConflict("任务不是可补图的逻辑计划")
            plan = await load_logical_plan(user_id, str(pointer["plan_id"]))
            if not plan:
                raise PlanPatchConflict("逻辑计划状态不可用")
            awaiting = {slot.id for slot in ready_slots(plan)}
            existing_patch = any(
                isinstance(item, dict) and item.get("patch_id") == patch.patch_id
                for item in (plan.get("history") or [])
            )
            terminal = job.status in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.INTERRUPTED,
            }
            if terminal and not existing_patch:
                raise PlanPatchConflict("终态任务不能追加计划节点")
            if terminal and existing_patch:
                entry = next(
                    item
                    for item in (plan.get("history") or [])
                    if isinstance(item, dict) and item.get("patch_id") == patch.patch_id
                )
                recorded = str(entry.get("request_fingerprint") or "")
                if recorded and recorded != patch_fingerprint(patch):
                    raise PlanPatchConflict("patch_id 已被不同内容的补丁使用")
                return PlanPatchAppendResult(
                    job=job,
                    patch_id=patch.patch_id,
                    slot_id=patch.slot_id,
                    revision=int(plan.get("revision") or 1),
                    replayed=True,
                    temporal_signaled=False,
                    requires_legacy_resume=False,
                )
            if not existing_patch and patch.slot_id not in awaiting:
                raise PlanPatchConflict("扩图插槽尚未满足上游依赖或不存在")

            # 重放按客户端原始补丁比对；首次提交会补齐安全字段和插槽依赖，
            # 因而不能拿规范化后的节点正文与客户端请求直接比较。
            if existing_patch:
                entry = next(
                    item
                    for item in (plan.get("history") or [])
                    if isinstance(item, dict) and item.get("patch_id") == patch.patch_id
                )
                recorded = str(entry.get("request_fingerprint") or "")
                if recorded and recorded != patch_fingerprint(patch):
                    raise PlanPatchConflict("patch_id 已被不同内容的补丁使用")
                prepared = patch
                changed = False
            else:
                prepared = self._prepare_patch(job, plan, patch)
                changed = apply_plan_patch(plan, prepared)
                if changed:
                    history = list(plan.get("history") or [])
                    if history and isinstance(history[-1], dict):
                        history[-1]["request_fingerprint"] = patch_fingerprint(patch)
                    plan["history"] = history
            if changed:
                await save_logical_plan(user_id, plan)

            job.routing = {
                **(job.routing or {}),
                "logical_plan": {
                    **pointer,
                    "revision": plan.get("revision", 1),
                    "progress": logical_plan_progress(plan),
                    "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
                },
                "plan_patch": {
                    "patch_id": prepared.patch_id,
                    "slot_id": prepared.slot_id,
                    "source": prepared.source,
                    "revision": plan.get("revision", 1),
                    "node_count": len(prepared.nodes),
                    "at": time.time(),
                },
            }
            runtime = str((job.routing or {}).get("runtime") or "")
            temporal = runtime in {"temporal_logical_read", "temporal_logical_effects"}
            # 只有由调度层置入的等待态可被补图自动唤醒，用户手动暂停必须
            # 继续等待用户显式恢复，避免外部事件绕过控制面意图。
            scheduler_wait = bool((job.routing or {}).get("scheduler_waiting_slots"))
            if temporal and scheduler_wait:
                job.status = JobStatus.RUNNING
                job.routing.pop("scheduler_waiting_slots", None)
            await self._store.save_job(job)

            if temporal and scheduler_wait:
                try:
                    await self._signal_temporal(job)
                except Exception as exc:  # noqa: BLE001
                    # 计划已成功持久化；恢复等待态后相同 patch_id 可安全重放，
                    # 从而再次发送 Signal，不把任务遗留在“运行中但无人唤醒”。
                    job.status = JobStatus.PAUSED
                    job.routing["scheduler_waiting_slots"] = [patch.slot_id]
                    await self._store.save_job(job)
                    raise PlanPatchConflict("补丁已持久化，但 Temporal 唤醒失败；请重试同一 patch_id") from exc
                return PlanPatchAppendResult(
                    job=job,
                    patch_id=prepared.patch_id,
                    slot_id=prepared.slot_id,
                    revision=int(plan.get("revision") or 1),
                    replayed=not changed,
                    temporal_signaled=True,
                    requires_legacy_resume=False,
                )
            return PlanPatchAppendResult(
                job=job,
                patch_id=prepared.patch_id,
                slot_id=prepared.slot_id,
                revision=int(plan.get("revision") or 1),
                replayed=not changed,
                temporal_signaled=False,
                requires_legacy_resume=(not temporal and scheduler_wait),
            )

    def _prepare_patch(self, job: Job, plan: dict[str, Any], patch: PlanPatch) -> PlanPatch:
        """规范化安全字段，并在落库前验证完整图与已注册 Worker。"""
        raw_slot = (plan.get("slots") or {}).get(patch.slot_id)
        if not isinstance(raw_slot, dict):
            raise PlanPatchConflict("扩图插槽不存在")
        slot = ExpansionSlot.model_validate(raw_slot)
        prepared_nodes: list[TaskNode] = []
        for raw_spec in patch.nodes:
            node = TaskNode.model_validate(raw_spec.model_dump(mode="json"))
            if not node.depends_on and slot.depends_on:
                node.depends_on = list(slot.depends_on)
            node.metadata = {
                **(node.metadata or {}),
                "scheduler_patch_id": patch.patch_id,
                "scheduler_slot_id": patch.slot_id,
                "plan_revision": int(plan.get("revision") or 1) + 1,
            }
            prepare_node_safety(node, job.user_id, job.job_id)
            prepared_nodes.append(node)

        existing = [
            TaskNode.model_validate((record or {}).get("node") or {})
            for record in (plan.get("nodes") or {}).values()
            if isinstance(record, dict)
        ]
        errors = validate_planned_dag([*existing, *prepared_nodes], self._workers)
        if errors:
            raise PlanPatchConflict("补图节点未通过编译校验: " + "；".join(errors[:5]))
        return patch.model_copy(
            update={
                "nodes": tuple(NodeSpec.model_validate(node.model_dump(mode="json")) for node in prepared_nodes)
            }
        )

    @staticmethod
    async def _signal_temporal(job: Job) -> None:
        runtime = str((job.routing or {}).get("runtime") or "")
        if runtime == "temporal_logical_read":
            from app.agents.orchestration.temporal.client import signal_logical_read_workflow

            await signal_logical_read_workflow(job.job_id, "plan_patch_available")
        elif runtime == "temporal_logical_effects":
            from app.agents.orchestration.temporal.client import signal_logical_effects_workflow

            await signal_logical_effects_workflow(job.job_id, "plan_patch_available")
