"""事件适配器：既有扁平事件 → 标准事件信封（**唯一转换点**）。

方案约束：

* 后端内部只生成**一套**标准事件；旧版/新版 SSE 由投影层分别渲染，
  业务代码（Orchestrator/Tool/Skill）不维护两套事件逻辑；
* 适配器是"旧字段 → 标准载荷"的唯一落点：进程日志的安全字段复用
  ``ProcessLogEntry.from_event``（已含路径/凭据/原始载荷净化），步骤/审批/产物
  只按**白名单**取展示字段；
* **``thought_delta`` 不传原始思维链**：本模块从不读取 ``reasoning`` /
  ``arguments`` / 原始 ``result``，并且标准信封在构造时还会再执行一次
  ``strip_unsafe_payload``（纵深防御）。

调用方只需要在 SSE 出口处调用 :func:`canonical_events`，其余代码保持现状。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lumi_contracts import (
    EventEnvelope,
    ProcessLogEntry,
    ProcessPayload,
    RunState,
    bounded_opaque_value,
    canonical_events_for,
    strip_unsafe_payload,
)
from lumi_contracts.events.process import (
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    sanitize_process_text,
)

#: 需要过程条目安全字段的事件（与 ``app.contracts.events`` 的集合保持一致方向）。
PROCESS_LIKE_EVENT_TYPES: frozenset[str] = frozenset({
    "process", "thinking", "step", "step_started", "step_completed", "plan_delta",
    "plan_ready", "tool", "tool_started", "tool_completed", "waiting_next",
    "capability_requested", "waiting_provider", "provider_connected",
    "provider_disconnected", "capability_started", "capability_completed",
    "capability_failed", "plugin_health_changed",
    "operation_started", "operation_preview", "operation_completed",
    "operation_failed", "operation_rolled_back",
    "approval_required", "approval_resolved",
})

#: 只做透传（类型不在收敛表内）的既有事件：仍需安全白名单收敛。
PASSTHROUGH_EVENT_TYPES: frozenset[str] = frozenset({
    "job", "task_router", "plan_ready", "plan_delta", "waiting_next", "warning",
    "capability_requested", "waiting_provider", "provider_connected",
    "provider_disconnected", "capability_started", "capability_completed",
    "capability_failed", "plugin_health_changed",
    "operation_started", "operation_preview", "operation_completed",
    "operation_failed", "operation_rolled_back",
})

#: ``control.state`` 合法取值（与 ``lumi_contracts.events.lifecycle.RunState`` 同词表）。
#: 终态帧上的 ``status`` / ``job_status`` 优先于类型推断：失败/取消的任务不能
#: 因为帧类型是 ``done`` 就被渲染成 completed。
CONTROL_STATES: frozenset[str] = frozenset(item.value for item in RunState)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _step_summary(status: str, title: str, step: Mapping[str, Any]) -> str:
    """步骤摘要兜底：与 ``orchestrator._step_frame_fields`` 同规则。

    只取**已经在外发字段里的安全文本**（``output`` / ``error`` / ``title``），
    再统一过 ``sanitize_process_text``；没有这些字段时给中性阶段文案。
    """
    key = str(status or "").strip().casefold()
    name = title or "当前步骤"
    if key in {"completed", "succeeded", "success"}:
        raw = step.get("output") or f"已完成{name}"
    elif key in {"failed", "error"}:
        raw = step.get("error") or f"{name}未完成"
    elif key in {"running", "retrying", "in_progress"}:
        raw = f"正在执行：{name}"
    else:
        raw = f"待执行：{name}"
    return sanitize_process_text(raw, limit=SUMMARY_MAX_CHARS)


def _step_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """步骤帧 → ``StepStartedPayload`` / ``StepCompletedPayload`` 的载荷（安全字段）。"""
    entry = ProcessLogEntry.from_event(dict(event))
    step = _as_dict(event.get("step"))
    result = _as_dict(event.get("result"))
    status = _text(event.get("status") or event.get("runtime_status") or step.get("status") or "running")
    display_summary = entry.summary or _step_summary(status, entry.title, step or event)
    duration = event.get("duration_ms") or step.get("duration_ms")
    artifact_refs = event.get("artifact_refs")
    if not isinstance(artifact_refs, list):
        artifact_refs = result.get("artifact_refs") if isinstance(result.get("artifact_refs"), list) else []
    result_ref = event.get("result_ref") or step.get("result_ref")
    return {
        "step_id": entry.step_id or entry.entry_id,
        "step_type": _text(event.get("step_type") or event.get("agent"))[:80],
        "name": entry.title[:TITLE_MAX_CHARS],
        "display_summary": display_summary,
        "tool_name": entry.tool_name,
        "status": status[:40],
        # 完整结果只留引用与摘要：事件里不出现原始输出正文。
        "output_summary": display_summary,
        "error_code": _text(event.get("error_code") or step.get("error_code"))[:80],
        "duration_ms": _int(duration),
        # 结果引用/产物引用是**不透明容器**：键名由各自契约定义，只清危险键 + 限体积。
        "result_ref": bounded_opaque_value(_as_dict(result_ref)),
        "artifact_refs": [
            bounded_opaque_value(_as_dict(item)) for item in artifact_refs[:20] if isinstance(item, Mapping)
        ],
    }


def _artifact_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """产物帧：只给引用与元数据（下载地址/令牌永远不进事件）。"""
    ref = _as_dict(event.get("artifact") or event.get("artifact_ref") or event)
    return {
        "artifact_id": _text(ref.get("artifact_id") or ref.get("ref_id") or ref.get("id"))[:120],
        "type": _text(ref.get("type") or ref.get("kind"))[:60],
        "filename": _text(ref.get("filename") or ref.get("name"))[:200],
        "mime_type": _text(ref.get("mime_type") or ref.get("media_type"))[:120],
        "size_bytes": ref.get("size_bytes") if ref.get("size_bytes") is not None else ref.get("size"),
        "expires_at": _text(ref.get("expires_at"))[:64],
    }


def _approval_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    step_id = _text(event.get("step_id") or event.get("node_id"))
    return {
        "request_id": _text(event.get("request_id") or event.get("approval_id") or event.get("call_id"))[:120],
        # 既有审批接口按 node_id 审批，事件里必须给全，前端不再用 step_id 顶替。
        "node_id": _text(event.get("node_id") or step_id)[:120],
        "step_id": step_id[:120],
        "capability": _text(event.get("capability"))[:120],
        "action": _text(event.get("action") or event.get("operation") or event.get("tool_name"))[:80],
        "target": _text(event.get("target") or event.get("logical_path") or event.get("path"))[:200],
        "risk_level": _text(event.get("risk_level") or event.get("risk"))[:40],
        "preview_ref": _text(event.get("preview_ref"))[:200],
        "expires_at": _text(event.get("expires_at"))[:64],
        "approved": bool(event.get("approved")) if event.get("approved") is not None else False,
        "resolved_by": _text(event.get("resolved_by"))[:80],
        "reason": sanitize_process_text(event.get("reason"), limit=SUMMARY_MAX_CHARS),
        "decided_at": _text(event.get("decided_at"))[:64],
    }


def _view_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    view = _as_dict(event.get("view") or event)
    return {
        "view_id": _text(view.get("view_id") or view.get("id"))[:120],
        "view_type": _text(view.get("view_type") or view.get("type"))[:40],
        "plugin_id": _text(view.get("plugin_id"))[:120],
        "plugin_version": _text(view.get("plugin_version"))[:40],
        "action": _text(view.get("action") or "upsert")[:40],
        "schema_version": _int(view.get("schema_version") or 1),
        "title": sanitize_process_text(view.get("title"), limit=TITLE_MAX_CHARS),
        # 视图数据是不透明容器（形状由视图契约定义）：只清危险键 + 限体积，
        # 否则 table 的 rows / chart 的 series 会被事件层白名单裁掉。
        "data": bounded_opaque_value(_as_dict(view.get("data"))),
        "data_ref": _text(view.get("data_ref"))[:200],
    }


def _error_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": _text(event.get("error_code") or event.get("code"))[:80],
        "message": sanitize_process_text(
            event.get("message") or event.get("error") or event.get("content"),
            limit=SUMMARY_MAX_CHARS,
        ),
        "retryable": bool(event.get("retryable")),
        "step_id": _text(event.get("step_id") or event.get("node_id"))[:120],
        "suggested_action": sanitize_process_text(event.get("suggested_action"), limit=SUMMARY_MAX_CHARS),
    }


def canonical_payload(event_type: str, event: Mapping[str, Any], *, status: str = "") -> dict[str, Any]:
    """按事件类型产出**标准载荷**（这是"旧字段 → 新字段"的唯一映射表）。"""
    text_type = str(event_type or "").strip()
    if text_type in {"delta", "text", "message", "text_delta"}:
        return {
            "content": str(event.get("content") or ""),
            "format": "markdown" if str(event.get("format") or "markdown").casefold() != "plain" else "plain",
            "message_id": _text(event.get("message_id"))[:120],
        }
    if text_type in {"process", "thinking"}:
        entry = ProcessLogEntry.from_event(dict(event), job_id=_text(event.get("job_id")))
        return ProcessPayload.from_entry(entry).model_dump(mode="json", exclude_none=True)
    if text_type in {"step", "step_started", "step_completed", "tool", "tool_started", "tool_completed"}:
        return _step_payload(event)
    if text_type in {"artifact", "artifact_created"}:
        return _artifact_payload(event)
    if text_type in {"view_updated", "view"}:
        return _view_payload(event)
    if text_type in {"approval_required", "approval_resolved"}:
        payload = _approval_payload(event)
        if text_type == "approval_resolved":
            return {
                key: payload[key]
                for key in ("request_id", "node_id", "step_id", "approved", "resolved_by", "reason", "decided_at")
            }
        return {
            key: payload[key]
            for key in (
                "request_id", "node_id", "step_id", "capability", "action", "target",
                "risk_level", "preview_ref", "expires_at",
            )
        }
    if text_type in {"error", "task_failed", "cancelled"}:
        return _error_payload(event)
    if text_type in {"done", "task_completed"}:
        # 终态帧上的状态优先：失败/取消/中断的任务不能因为类型是 done 就报 completed。
        state = _text(
            event.get("state") or event.get("job_status") or event.get("status")
        ).casefold()
        if state not in CONTROL_STATES:
            state = "completed"
        return {
            "state": state,
            "reason_code": _text(event.get("reason_code") or event.get("error_code"))[:80],
            "next_action": _text(event.get("next_action"))[:80],
        }
    # 过程类/能力类/操作类：统一走过程安全字段，再叠加本帧自己的展示字段。
    payload: dict[str, Any] = {}
    if text_type in PROCESS_LIKE_EVENT_TYPES:
        entry = ProcessLogEntry.from_event(dict(event))
        payload = ProcessPayload.from_entry(entry).model_dump(mode="json", exclude_none=True)
    payload.update(strip_unsafe_payload(dict(event)))
    if status:
        payload.setdefault("status", status[:40])
    return payload


def canonical_events(
    event: Mapping[str, Any],
    *,
    job_id: str = "",
    conversation_id: str = "",
    trace_id: str = "",
) -> list[EventEnvelope]:
    """既有扁平事件 → 标准事件（1..2 帧，含终态兼容伴随帧）。

    ``seq`` 由 SSE 编码器分配（:meth:`EventEnvelope.with_seq`），本函数不持有
    流计数器，因此同一事件在任何出口都得到完全相同的标准形态。
    """
    data = dict(event or {})
    event_type = str(data.get("type") or "error")
    status = _text(data.get("status") or data.get("runtime_status"))
    resolved_job_id = str(data.get("job_id") or job_id or "")
    payload = canonical_payload(event_type, data, status=status)
    return canonical_events_for(
        event_type,
        payload,
        job_id=resolved_job_id,
        conversation_id=str(data.get("conversation_id") or conversation_id or ""),
        trace_id=str(data.get("trace_id") or trace_id or ""),
        occurred_at=_text(data.get("occurred_at")),
        status=status,
    )


__all__ = [
    "PASSTHROUGH_EVENT_TYPES",
    "PROCESS_LIKE_EVENT_TYPES",
    "canonical_events",
    "canonical_payload",
]
