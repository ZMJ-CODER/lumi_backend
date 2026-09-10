"""Job 运行视图与计划式事件载荷的纯构建层。

供以下路径共用同一投影，避免各处手写字段漂移：
  - GET /agents/jobs/{id}（页面刷新后恢复 计划/步骤/当前状态/按钮数据）；
  - plan_ready / waiting_next / done 等 SSE 事件载荷；
  - 前端 jobRunLive 与 JobBubble 按钮状态机。

Job 对象与 routing 中只保存规范执行状态（execution_state）与现有 JobStatus
最近邻；本模块负责投影出前端契约（execution_mode / plan_revision /
current_step_index / steps / canonical status）。
"""

from __future__ import annotations

import time
from typing import Any

from lumi_orch.execution_mode import (
    EXECUTION_DIRECT,
    EXECUTION_JOB_STATES,
    resolve_execution_mode,
)

# 现有 JobStatus 值 → 前端规范状态（无 execution_state 时的回退）。
_EXISTING_TO_CANONICAL = {
    "pending": "planning",
    "running": "running_step",
    "completed": "completed",
    "failed": "failed",
    "interrupted": "failed",
    "cancelled": "cancelled",
    "waiting_approval": "waiting_approval",
    "waiting_resources": "waiting_run",
}

# canonical 状态 → 前端下一步按钮（run_next 前端契约 next_action）。
_NEXT_ACTION_BY_STATE = {
    "planning": "none",
    "waiting_run": "run",
    "waiting_next": "run_next",
    "running_step": "none",
    "waiting_approval": "wait_approval",
    "completed": "none",
    "failed": "retry",
    "cancelled": "none",
}


def _attr(obj: Any, key: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def canonical_state_for(job: Any) -> str:
    """由 Job 对象推导前端规范状态：优先 routing.execution_state。"""
    routing = _attr(job, "routing", {}) or {}
    if not isinstance(routing, dict):
        routing = {}
    state = str(routing.get("execution_state") or "").strip()
    if state in EXECUTION_JOB_STATES:
        return state
    existing = str(_attr(job, "status", "") or "").lower()
    return _EXISTING_TO_CANONICAL.get(existing, "planning")


def next_action_for_state(state: str) -> str:
    """按 canonical 状态给出前端按钮语义（run/run_next/wait_approval/retry/none）。"""
    return _NEXT_ACTION_BY_STATE.get(str(state or "").strip(), "none")


def run_view(
    job: Any,
    *,
    plan_text: str = "",
    dsml_pending: bool = False,
    status_override: str = "",
) -> dict:
    """把 Job（或其快照）投影为前端可恢复的运行视图。

    返回契约字段与旧字段并存（旧 job 快照兼容入口）：execution_mode /
    plan_revision / current_step_index / steps / canonical status /
    next_action / updated_at / final_answer / task_completed / task_failed。
    """
    routing = _attr(job, "routing", {}) or {}
    routing = routing if isinstance(routing, dict) else {}
    grant = routing.get("workspace_grant") if isinstance(routing.get("workspace_grant"), dict) else {}
    execution_mode = str(routing.get("execution_mode") or "").strip()
    if not execution_mode:
        execution_mode = resolve_execution_mode(approval_mode=str(grant.get("approval_mode") or ""))
    canonical = status_override or canonical_state_for(job)
    steps = routing.get("steps")
    if not isinstance(steps, list):
        steps = []
    result = _attr(job, "result", {})
    if not isinstance(result, dict):
        result = {}
    final_answer = str(result.get("final_answer") or result.get("answer") or "").strip()[:20000]
    view_steps: list[dict] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            continue
        step = dict(raw)
        step["index"] = int(index)
        view_steps.append(step)
    return {
        "job_id": str(_attr(job, "job_id", "") or ""),
        "execution_mode": execution_mode,
        "status": canonical,
        "plan_revision": int(routing.get("plan_revision") or 1),
        "current_step_index": int(routing.get("current_step_index") or 0),
        "next_action": next_action_for_state(canonical),
        "steps": view_steps,
        "plan_text": str(plan_text or routing.get("plan_text") or "")[:12000],
        "dsml_pending": bool(dsml_pending),
        "final_answer": final_answer,
        "updated_at": float(_attr(job, "updated_at", time.time()) or time.time()),
        "task_completed": canonical == "completed",
        "task_failed": canonical == "failed",
        "direct": execution_mode == EXECUTION_DIRECT,
    }


def steps_from_nodes(nodes: list[Any]) -> list[dict]:
    """把已编译计划节点投影为人可见的 execution steps（JSON-safe）。

    每个步骤只含 id/title/description/domain/status/result_ref，不带完整
    工具参数与历史；作为 routing["steps"] 的持久来源供前端恢复与单步执行。
    """
    steps: list[dict] = []
    for node in nodes:
        params = _attr(node, "params", {}) if not isinstance(node, dict) else (node.get("params") or {})
        if not isinstance(params, dict):
            params = {}
        node_id = str(_attr(node, "id", "") or "")
        title = str(_attr(node, "name", "") or params.get("step_title") or "步骤")
        description = str(
            params.get("instruction")
            or params.get("skill_name")
            or params.get("step_title")
            or title
        )
        domain = str(getattr(node, "agent", "") or params.get("domain") or "workspace") if not isinstance(node, dict) else str(node.get("agent") or params.get("domain") or "workspace")
        steps.append({
            "id": node_id,
            "title": title[:200],
            "description": description[:1000],
            "domain": domain[:40],
            "status": "pending",
            "result_ref": None,
        })
    return steps


def plan_ready_payload(*, job_id: str, view: dict) -> dict:
    """plan_ready 事件载荷：展示计划，等待用户运行（waiting_run）。"""
    return {
        "job_id": str(job_id),
        "execution_mode": view["execution_mode"],
        "status": view["status"],
        "plan_revision": view["plan_revision"],
        "steps": view["steps"],
        "plan_text": view["plan_text"],
        "run_view": view,
    }


def waiting_next_payload(
    *,
    job_id: str,
    view: dict,
    completed_step_id: str = "",
    next_step_id: str = "",
) -> dict:
    """waiting_next 事件载荷：跑完一步后等待“运行下一步”。"""
    steps = view.get("steps") or []
    next_index = -1
    for step in steps:
        if isinstance(step, dict) and str(step.get("id") or "") == next_step_id:
            next_index = int(step.get("index") or -1)
            break
    status = (
        "waiting_next"
        if view["status"] not in {"completed", "failed", "cancelled"}
        else view["status"]
    )
    return {
        "job_id": str(job_id),
        "status": status,
        "completed_step_id": str(completed_step_id or ""),
        "next_step_id": str(next_step_id or ""),
        "next_step_index": next_index,
        "plan_revision": view["plan_revision"],
        "button_label": "运行下一步",
        "run_view": view,
    }


def done_payload(
    *,
    job_id: str,
    view: dict,
    message_id: str = "",
    content: str = "",
) -> dict:
    """done 事件载荷：job_status/run_view/plan_text 对齐前端契约。

    保留旧聊天字段（message_id/content）以兼容既有 /chat/stream 消费路径。
    """
    return {
        "type": "done",
        "message_id": str(message_id or ""),
        "content": str(content or "")[:20000],
        "job_id": str(job_id),
        "status": view["status"],
        "job_status": view["status"],
        "execution_mode": view["execution_mode"],
        "plan_revision": view["plan_revision"],
        "plan_text": view["plan_text"],
        "dsml_pending": view["dsml_pending"],
        "run_view": view,
    }
