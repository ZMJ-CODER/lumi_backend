"""v2 验收追踪：单请求级 SSE 证据记录（灰度 EXECUTION_POLICY_V2_ENABLED）。

联调时只需开启开关并在每个用例后查看日志中的
``policy_v2_acceptance`` JSON 行，即可一次性核对：
  - 画像/策略（task_profile、execution_policy、policy_version）；
  - 原始 SSE 事件顺序（type + 关键字段，不含正文全文，仅记字符量）；
  - done 数量与唯一性、delta 分段数量；
  - first_delta 时机（process/tool 不计入首字）、route 与 stream 时长；
  - Planner / Agent / 工具调用是否发生。

纯逻辑、不依赖模型/Redis：开关关闭时所有调用为 no-op。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

TRACE_KEY = "policy_v2_acceptance"

# 表示进入 Planner/DAG 的任务级事件；出现即计入 planner_invoked/agent_invoked。
_JOB_EVENTS = frozenset({"job", "plan_ready", "plan_delta", "plan"})
_AGENT_EVENTS = frozenset({"step", "step_started", "step_completed", "task_completed"})
_TOOL_EVENTS = frozenset({"tool_started", "tool_completed", "tool"})


def new_trace(*, enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    return {
        "started_at": time.time(),
        "first_response_at": None,
        "first_delta_at": None,
        "last_delta_at": None,
        "done_at": None,
        "event_types": [],
        "delta_count": 0,
        "delta_chars": 0,
        "done_count": 0,
        "tool_count": 0,
        "planner_event_seen": False,
        "agent_event_seen": False,
    }


def record_trace_event(trace: dict[str, Any] | None, event: dict) -> None:
    """记录一个 SSE 事件的关键字段（不记录正文全文，只记字符量）。

    连续 delta 在事件序列里折叠为一个 `delta×N`（只保留首个），避免长答复把
    真正的关键事件（tool/done/error）淹没；计数仍逐条累计。
    """
    if trace is None:
        return
    event_type = str(event.get("type") or "")
    now = time.time()
    if event_type == "delta" and trace.get("last_type") == "delta":
        trace["delta_run"] = int(trace.get("delta_run") or 1) + 1
    else:
        if trace.get("last_type") == "delta":
            _fold_delta_run(trace)
        trace.setdefault("event_types", []).append(event_type)
        trace["delta_run"] = 1 if event_type == "delta" else 0
    trace["last_type"] = event_type
    if event_type not in {"task_policy", "task_router", "done"} and trace.get("first_response_at") is None:
        trace["first_response_at"] = now
    if event_type == "delta":
        trace["delta_count"] = int(trace.get("delta_count") or 0) + 1
        trace["delta_chars"] = int(trace.get("delta_chars") or 0) + len(
            str(event.get("content") or "")
        )
        if trace.get("first_delta_at") is None:
            trace["first_delta_at"] = now
        trace["last_delta_at"] = now
    elif event_type == "done":
        trace["done_count"] = int(trace.get("done_count") or 0) + 1
        trace["done_at"] = now
    if event_type in _TOOL_EVENTS:
        trace["tool_count"] = int(trace.get("tool_count") or 0) + 1
    if event_type in _JOB_EVENTS:
        trace["planner_event_seen"] = True
    if event_type in _AGENT_EVENTS or event_type in _JOB_EVENTS:
        trace["agent_event_seen"] = True


def _fold_delta_run(trace: dict[str, Any]) -> None:
    """把序列末尾的裸 `delta` 折叠成 `delta×N`。"""
    run = int(trace.get("delta_run") or 0)
    types = trace.get("event_types") or []
    if run > 1 and types and types[-1] == "delta":
        types[-1] = f"delta×{run}"
    trace["delta_run"] = 0


def finish_trace(
    trace: dict[str, Any] | None,
    *,
    user_id: str = "",
    conversation_id: str = "",
    policy_public: dict[str, Any] | None = None,
    planner_hint: bool = False,
    agent_hint: bool = False,
    workspace_read_seconds: float | None = None,
    workspace_tool_count: int = 0,
    router_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """汇总并输出一条验收 JSON 日志（供联调抓取证据）。"""
    if trace is None:
        return None
    _fold_delta_run(trace)
    first_delta = trace.get("first_delta_at")
    router_profile = (router_meta or {}).get("task_profile") or {}
    raw_decision = (policy_public or {}).get("route_decision")
    route_decision = (
        {
            key: raw_decision.get(key)
            for key in ("schema_name", "schema_version", "policy_version", "route_mode")
        }
        if isinstance(raw_decision, dict)
        else None
    )
    summary: dict[str, Any] = {
        "user_id": str(user_id or "")[:40],
        "conversation_id": str(conversation_id or "")[:40],
        # 严格 Router v2 画像（单一事实来源）
        "route_mode": (router_meta or {}).get("route_mode"),
        "safety_action": (router_meta or {}).get("safety_action"),
        "assessor_source": (router_meta or {}).get("assessor_source"),
        "router_profile": {
            key: router_profile.get(key)
            for key in ("complexity", "confidence", "side_effects", "info_sources",
                        "output_target", "execution_target", "risk_level")
            if key in router_profile
        },
        # 权威画像/路由（Router v2 时为严格 M0-M3 画像；旧画像在 compat 里）
        "task_profile": (policy_public or {}).get("task_profile"),
        "execution_policy": (policy_public or {}).get("execution_policy"),
        # 契约版本唯一读取点：routing.route_decision.policy_version
        "policy_version": (policy_public or {}).get("policy_version"),
        "route_decision": route_decision,
        "compat": (policy_public or {}).get("compat"),
        "event_sequence": list(trace.get("event_types") or []),
        "done_count": int(trace.get("done_count") or 0),
        "delta_count": int(trace.get("delta_count") or 0),
        "delta_chars": int(trace.get("delta_chars") or 0),
        "tool_event_count": int(trace.get("tool_count") or 0),
        "planner_invoked": bool(
            trace.get("planner_event_seen") or planner_hint
        ),
        "agent_invoked": bool(trace.get("agent_event_seen") or agent_hint),
        "route_latency_ms": (
            int(trace["route_latency_ms_override"])
            if trace.get("route_latency_ms_override") is not None
            else _ms(trace.get("started_at"), trace.get("first_response_at"))
        ),
        "first_delta_latency_ms": _ms(trace.get("started_at"), first_delta),
        "answer_stream_duration_ms": _ms(first_delta, trace.get("last_delta_at")),
        "workspace_read_duration_ms": _ms(0, workspace_read_seconds)
        if workspace_read_seconds is not None else None,
        "workspace_tool_count": int(workspace_tool_count or 0),
    }
    logger.info(TRACE_KEY + " {}", json.dumps(summary, ensure_ascii=False, default=str))
    return summary


def _ms(start: float | None, end: float | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start) * 1000))


def sse_detail(event: dict) -> str:
    """单条 SSE 事件的紧凑摘要（正文只记长度，不落全文）。"""
    event_type = str(event.get("type") or "")
    if event_type == "delta":
        return f"chars={len(str(event.get('content') or ''))}"
    if event_type == "process":
        return f"content={str(event.get('content') or '')[:60]!r}"
    if event_type == "task_router":
        return (
            f"route_mode={event.get('route_mode')} safety={event.get('safety_action')} "
            f"complexity={((event.get('task_profile') or {}).get('complexity'))}"
        )
    if event_type == "task_policy":
        return f"execution_policy={event.get('execution_policy')}"
    if event_type in {"tool_started", "tool_completed"}:
        return f"tool={event.get('tool')} status={event.get('status')}"
    if event_type in {"step_started", "step_completed"}:
        return f"step_id={event.get('step_id')} status={event.get('status')}"
    if event_type == "waiting_next":
        return f"next_step_id={event.get('next_step_id')}"
    if event_type == "task_failed":
        return f"error_code={event.get('error_code')}"
    if event_type == "task_completed":
        return f"final_chars={len(str(event.get('final_answer') or ''))}"
    if event_type == "done":
        return (
            f"status={event.get('status')} content_chars={len(str(event.get('content') or ''))}"
        )
    if event_type == "job":
        return f"job_id={event.get('job_id')}"
    if event_type == "plan_ready":
        return f"status={event.get('status')}"
    if event_type == "error":
        return f"code={event.get('code')}"
    return ""


def log_sse_start(*, conversation_id: str, content: str, scene: str) -> None:
    """验收模式：请求开始标记（含问题前 80 字，便于对应用例）。"""
    logger.info(
        "acceptance_sse_start {}",
        json.dumps(
            {
                "conversation_id": str(conversation_id or "")[:64],
                "scene": str(scene or ""),
                "request": str(content or "")[:80],
            },
            ensure_ascii=False,
        ),
    )


def log_sse_event(*, conversation_id: str, sequence: int, elapsed_ms: int, event: dict) -> None:
    """验收模式：把一条 SSE 事件写入后端日志（默认关闭时调用方不会调用）。"""
    logger.info(
        "acceptance_sse {}",
        json.dumps(
            {
                "conversation_id": str(conversation_id or "")[:64],
                "seq": int(sequence),
                "ms": int(elapsed_ms),
                "type": str(event.get("type") or ""),
                "detail": sse_detail(event),
            },
            ensure_ascii=False,
        ),
    )
