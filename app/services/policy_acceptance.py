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
    """记录一个 SSE 事件的关键字段（不记录正文全文，只记字符量）。"""
    if trace is None:
        return
    event_type = str(event.get("type") or "")
    now = time.time()
    trace.setdefault("event_types", []).append(event_type)
    if event_type not in {"task_policy", "done"} and trace.get("first_response_at") is None:
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
) -> dict[str, Any] | None:
    """汇总并输出一条验收 JSON 日志（供联调抓取证据）。"""
    if trace is None:
        return None
    first_delta = trace.get("first_delta_at")
    summary: dict[str, Any] = {
        "user_id": str(user_id or "")[:40],
        "conversation_id": str(conversation_id or "")[:40],
        "task_profile": (policy_public or {}).get("task_profile"),
        "execution_policy": (policy_public or {}).get("execution_policy"),
        "policy_version": (policy_public or {}).get("policy_version"),
        "event_sequence": list(trace.get("event_types") or []),
        "done_count": int(trace.get("done_count") or 0),
        "delta_count": int(trace.get("delta_count") or 0),
        "delta_chars": int(trace.get("delta_chars") or 0),
        "tool_event_count": int(trace.get("tool_count") or 0),
        "planner_invoked": bool(
            trace.get("planner_event_seen") or planner_hint
        ),
        "agent_invoked": bool(trace.get("agent_event_seen") or agent_hint),
        "route_latency_ms": _ms(trace.get("started_at"), trace.get("first_response_at")),
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
