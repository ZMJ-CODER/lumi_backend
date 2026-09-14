"""Step 状态适配：Job/节点形状 ↔ 执行内核的 ``StepRunState``。

这一层是**纯适配**，不做 IO、不发事件：

* ``locate_current_step``：对外兼容入口（实现委托给内核 ``step_engine``）；
* ``_state_from_job``：Job + routing → ``StepRunState``（依赖是否满足在这里算）；
* ``_current_step_id`` / ``_effect_type_for_node``：检查点与投影要的稳定取值，
  判不出就返回空串/``unknown``——**不猜**。
"""

from __future__ import annotations

from typing import Any

from lumi_execution.step_contract import StepRunState, locate_next_step

from app.agents.orchestration.models import Job, TaskNode, TaskStatus


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


def _current_step_id(state: StepRunState) -> str:
    """当前步骤 id（越界/形状异常一律返回空串，不猜）。"""
    index = int(state.current_step_index or 0)
    steps = state.steps or []
    if 0 <= index < len(steps) and isinstance(steps[index], dict):
        return str(steps[index].get("id") or steps[index].get("step_id") or "")
    return ""


def _effect_type_for_node(node: Any) -> str:
    """按节点声明判定副作用类型（判不出返回 ``unknown``，不猜）。"""
    if node is None:
        return ""
    try:
        from lumi_orch.effects import effect_type_for

        params = getattr(node, "params", {}) or {}
        return str(
            effect_type_for(
                params.get("preferred_tool"),
                params.get("tool"),
                params.get("action"),
                params.get("operation"),
                getattr(node, "agent", ""),
            ).value
        )
    except Exception:  # noqa: BLE001 - 判不出类型不能影响检查点写入
        return ""


__all__ = [
    "_current_step_id",
    "_dependencies_done",
    "_effect_type_for_node",
    "_state_from_job",
    "locate_current_step",
]
