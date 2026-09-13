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
    CANONICAL_EVENT_TYPES,
    KNOWN_EVENT_TYPES,
    EventEnvelope,
    ProcessLogEntry,
    ProcessPayload,
    RunState,
    bounded_opaque_value,
    build_payload,
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

#: "结构可解释"的事件类型：收敛表 + 既有已知集合 + 过程类 + 透传类 + 终态别名。
#: 不在这张表里的类型，其载荷结构**不解析**（方案 §1.3 未知事件策略）。
KNOWN_STRUCTURED_EVENT_TYPES: frozenset[str] = frozenset(
    set(CANONICAL_EVENT_TYPES)
    | set(KNOWN_EVENT_TYPES)
    | set(PROCESS_LIKE_EVENT_TYPES)
    | set(PASSTHROUGH_EVENT_TYPES)
    | {
        "text_delta", "capability_completed", "capability_failed",
        "done", "task_completed", "task_failed", "cancelled", "job_cancelled",
        "warning", "view", "view_updated", "artifact", "artifact_created",
    }
)


def _is_unknown_structure(event_type: str, event: Mapping[str, Any]) -> bool:
    """类型未登记 **且** 载荷键超出安全白名单 → 结构不可解释（走未知事件策略）。"""
    if str(event_type or "").strip() in KNOWN_STRUCTURED_EVENT_TYPES:
        return False
    from lumi_contracts.events.envelope import ALLOWED_PAYLOAD_KEYS

    return not set(map(str, event.keys())) <= set(ALLOWED_PAYLOAD_KEYS)


#: 纯路由元数据键：判断"这帧还有没有可解释的载荷"时忽略它们
#: （只有 ``type`` / ``job_id`` 这类信封级字段不算内容）。
_FRAME_METADATA_KEYS: frozenset[str] = frozenset({
    "type", "version", "seq", "job_id", "conversation_id", "trace_id",
    "occurred_at", "call_id", "step_id",
})


def _unknown_event_payload(event_type: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """未知事件：可解释的安全字段仍透传，但打 ``unsupported`` 标记。

    方案 §1.3 的"不解析正文"落在这里：白名单之外的结构一个都不读；如果连一个已登记
    的安全字段都没有（结构完全不可解释），小数据降级为空载荷、大对象只留哈希引用。
    """
    safe = strip_unsafe_payload(dict(event))
    content = {key: value for key, value in safe.items() if key not in _FRAME_METADATA_KEYS}
    if content:
        # 前端容忍未知类型（不得白屏）：保留安全展示字段，同时明确标记"本客户端
        # 不支持该事件"，避免被当成已知事件渲染。
        return {**safe, "unsupported": True, "schema_version": 0}
    from lumi_contracts.events.registry import decide_unknown_event

    decision = decide_unknown_event(
        event_type, dict(event), known_types=KNOWN_STRUCTURED_EVENT_TYPES
    )
    if decision.action == "pass":
        return safe
    return decision.payload


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
    payload = {
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
    # 统一资源能力层（§七）：步骤帧里给出**结构化标签**，前端不必按工具名猜
    # "这是在读工作区还是在写办公文档"。只放闭集词汇（`label_value` 形状闸门），
    # 缺失就不出现——老客户端/未知工具不会因此多出字段。
    from app.contracts.process_log import dispatch_labels_for_step

    payload.update(dispatch_labels_for_step(step or dict(event), entry.tool_name))
    return payload


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
    """错误帧 → 统一错误载荷（``UnifiedError`` 的事件形态，方案 §3）。

    只有 ``safe_message`` 会到前端：原始异常文本/供应商响应/堆栈一律不进载荷，
    完整信息进 ``detail_ref`` 指向的产物。同一类失败从任何路径得到同一个
    ``code`` + ``safe_message``（映射表只有 :mod:`lumi_contracts.events.errors` 一份）。
    """
    from lumi_contracts.events.errors import translate_error

    raw_code = _text(event.get("error_code") or event.get("code") or event.get("reason_code"))
    if not raw_code:
        # 取消类帧没有"错误码"是正常的：收敛为冻结码 ``SYSTEM_CANCELLED``，
        # 而不是掉进 system.internal（前端文案完全不同）。
        frame_type = _text(event.get("type"))
        if frame_type in {"cancelled", "job_cancelled", "system_cancelled"}:
            raw_code = "SYSTEM_CANCELLED"
    unified = translate_error(
        {
            "code": raw_code,
            "retryable": event.get("retryable") if isinstance(event.get("retryable"), bool) else None,
            "step_id": _text(event.get("step_id") or event.get("node_id")),
            "detail_ref": _text(event.get("detail_ref")),
            "suggested_action": _text(event.get("suggested_action")),
        }
    )
    payload = unified.to_payload()
    # ``control`` 载荷用 ``error_code`` 承载同一个码（失败/取消的终态帧）。
    payload["error_code"] = unified.code
    payload.setdefault("safe_next_action", unified.suggested_action)
    return payload


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
    if text_type in {"error", "task_failed", "failed", "cancelled", "job_cancelled"}:
        return _error_payload(event)
    if text_type == "control":
        # 方案 4 §6.1：预检的 ``blocked`` / ``waiting_clarification`` 也走 ``control``，
        # 因此这里必须按 ``ControlPayload`` 白名单**显式**收敛，否则追问文本、选项、
        # 工具窗口会被通用兜底路径丢掉，前端只剩一个没有解释的"被阻断"。
        values = {
            "state": _text(event.get("state") or event.get("status") or "running")[:40],
            "error_code": _text(event.get("error_code") or event.get("code"))[:80],
            "reason_code": _text(event.get("reason_code"))[:80],
            "reason": _text(event.get("reason"))[:400],
            "next_action": _text(event.get("next_action"))[:80],
            "safe_next_action": _text(event.get("safe_next_action"))[:80],
            "phase": _text(event.get("phase"))[:40],
            "question": _text(event.get("question"))[:400],
            "options": [str(item)[:40] for item in (event.get("options") or []) if str(item)][:10],
            "required_capabilities": [
                str(item)[:120] for item in (event.get("required_capabilities") or []) if str(item)
            ][:20],
            "tool_window": [str(item)[:120] for item in (event.get("tool_window") or []) if str(item)][:20],
        }
        if isinstance(event.get("must_call_model"), bool):
            values["must_call_model"] = bool(event["must_call_model"])
        if isinstance(event.get("retryable"), bool):
            values["retryable"] = bool(event["retryable"])
        payload = build_payload("control", values)
        # ``safe_message`` 不在 ControlPayload 里（它在 ``EventPayload`` 层），
        # 但预检的用户文案必须能到前端：放进 ``reason``（同语义、同白名单）。
        if not payload.get("reason"):
            payload["reason"] = _text(event.get("safe_message"))[:400]
        if not payload.get("safe_next_action"):
            payload["safe_next_action"] = _text(event.get("suggested_action"))[:80]
        return payload
    if text_type in {"done", "task_completed"}:
        # 终态帧上的状态优先：失败/取消/中断的任务不能因为类型是 done 就报 completed。
        state = _text(
            event.get("state") or event.get("job_status") or event.get("status")
        ).casefold()
        if state not in CONTROL_STATES:
            state = "completed"
        payload: dict[str, Any] = {
            "state": state,
            "reason_code": _text(event.get("reason_code") or event.get("error_code"))[:80],
            "next_action": _text(event.get("next_action"))[:80],
        }
        if state in {"failed", "cancelled", "interrupted"}:
            # 失败/取消的终态帧必须带**统一错误码**与"下一步"文案，前端据此分派。
            payload.update(_error_payload(event))
            payload["state"] = state
        return payload
    # 过程类/能力类/操作类：统一走过程安全字段，再叠加本帧自己的展示字段。
    payload: dict[str, Any] = {}
    if text_type in PROCESS_LIKE_EVENT_TYPES:
        entry = ProcessLogEntry.from_event(dict(event))
        payload = ProcessPayload.from_entry(entry).model_dump(mode="json", exclude_none=True)
    elif _is_unknown_structure(text_type, event):
        # 未注册类型 + 看不懂的载荷结构（方案 §1.3）：小数据不解析、大对象只留哈希引用，
        # 绝不把看不懂的正文塞进公开事件。
        return _unknown_event_payload(text_type, event)
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
