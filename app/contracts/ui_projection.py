"""``ExecutionResult`` → UI 事件投影（阶段 3：结果归一化链路的出口）。

链路固定为：

    原始结果 → ExecutionResult → UI Projection → Stream Event

不允许的路径（本模块存在的理由）：``ToolOutput`` 直接 ``json.dumps`` 上 SSE、
``SkillResult`` 直接拼字符串给前端、模型原文直接进气泡。

实现要点：

* **复用既有 UI 投影**（``lumi_contracts.projections.UiProjection``），不新增第二套
  展示字段定义；本模块只负责把 UI 视图 + 控制面字段折成标准事件；
* **完整结果只留引用**：事件里给 ``result_ref`` 与 ``output_summary``，
  不塞原始 payload/正文；
* ``pending_approval`` → ``approval_required``（审批结果仍走既有审批 REST）；
* 失败/取消 → ``error`` + ``step_completed(status=failed)``，不新造错误结构。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts import (
    EventEnvelope,
    ExecutionResult,
    build_payload,
)
from lumi_contracts.events.envelope import (
    EVENT_ENVELOPE_VERSION,
    ControlPayload,
    ErrorPayload,
    StepCompletedPayload,
)
from lumi_contracts.events.process import SUMMARY_MAX_CHARS, sanitize_process_text

#: 执行状态 → 标准控制态（``control.state`` 词表）。
_STATUS_TO_CONTROL_STATE: dict[str, str] = {
    "pending": "running",
    "running": "running",
    "pending_approval": "running",
    "success": "completed",
    "partial": "completed",
    "empty": "completed",
    "no_change": "completed",
    "already_absent": "completed",
    "failed": "failed",
    "denied": "failed",
    "cancelled": "cancelled",
    "uncertain": "failed",
}


def ui_view(result: ExecutionResult[Any]) -> dict[str, Any]:
    """执行结果 → UI 视图（既有投影；失败时退化为最小安全视图）。"""
    try:
        from app.contracts import project

        view = project(result, "ui")
        return view if isinstance(view, dict) else {}
    except Exception:  # noqa: BLE001 - 投影失败不得让事件消失
        return {
            "kind": "ui",
            "tool": str(getattr(result, "tool_name", "") or ""),
            "call_id": str(getattr(result, "call_id", "") or ""),
            "status": str(getattr(result, "status", "") or ""),
        }


def result_ref(result: ExecutionResult[Any], *, step_id: str = "") -> dict[str, Any]:
    """结果引用：指向"完整结果在哪"，本身不含正文。

    现阶段结果随 Job/步骤快照存活，因此引用由 ``job_id + node_id/call_id + schema``
    组成；接入独立结果存储时只需扩展本函数，事件形状不变。
    """
    return {
        "job_id": str(getattr(result, "job_id", "") or ""),
        "step_id": str(step_id or getattr(result, "node_id", "") or ""),
        "call_id": str(getattr(result, "call_id", "") or ""),
        "tool": str(getattr(result, "tool_name", "") or ""),
        "schema": str(getattr(result, "schema_name", "") or ""),
        "schema_version": int(getattr(result, "schema_version", 1) or 1),
    }


def artifact_refs_of(result: ExecutionResult[Any]) -> list[dict[str, Any]]:
    """产物引用（只保留可展示元数据；``internal_locator`` 永不外发）。

    优先取 ``metadata["artifacts"]``（结果归一化时带上的安全展示元数据，含
    ``expires_at``）；没有时退回契约 ``artifact_refs``（字段略少但形状一致）。
    """
    metadata = getattr(result, "metadata", None)
    safe = metadata.get("artifacts") if isinstance(metadata, dict) else None
    if isinstance(safe, list) and safe:
        return [dict(item) for item in safe if isinstance(item, dict)]
    out: list[dict[str, Any]] = []
    for ref in getattr(result, "artifact_refs", None) or []:
        data = ref.model_dump(mode="json", exclude_none=True) if hasattr(ref, "model_dump") else dict(ref)
        out.append({
            "artifact_id": str(data.get("ref_id") or ""),
            "type": str(data.get("schema_name") or ""),
            "filename": str(data.get("name") or ""),
            "mime_type": str(data.get("media_type") or ""),
            "size_bytes": data.get("size"),
        })
    return out


def execution_result_events(
    result: ExecutionResult[Any],
    *,
    step_id: str = "",
    job_id: str = "",
) -> list[EventEnvelope]:
    """执行结果 → 标准事件（``step_completed`` / ``artifact_created`` / ``approval_required`` / ``error``）。

    返回值可能包含多帧（一个有产物的成功结果 = step_completed + N×artifact_created）；
    ``seq`` 由调用方的编码器统一分配。
    """
    view = ui_view(result)
    status = str(getattr(result, "status", "") or "")
    tool_name = str(getattr(result, "tool_name", "") or "")
    raw_step = str(step_id or getattr(result, "node_id", "") or getattr(result, "call_id", "") or tool_name)
    error = getattr(result, "error", None)
    duration_ms = int(getattr(getattr(result, "timing", None), "duration_ms", 0) or 0)
    summary = sanitize_process_text(
        view.get("summary") or getattr(result, "output", ""), limit=SUMMARY_MAX_CHARS
    )
    refs = artifact_refs_of(result)

    base = {
        "job_id": str(job_id or getattr(result, "job_id", "") or ""),
        "conversation_id": "",
        "trace_id": str(getattr(result, "trace_id", "") or ""),
        "occurred_at": "",
    }

    completed_values = {
        "step_id": raw_step,
        "status": "completed" if str(getattr(result, "status", "")).lower() in {"success", "partial", "empty", "no_change", "already_absent"} else "failed",
        "duration_ms": duration_ms,
        "output_summary": summary,
        "error_code": str(getattr(error, "code", "") or ""),
        "result_ref": result_ref(result, step_id=raw_step),
        "artifact_refs": refs,
    }
    events = [
        EventEnvelope(
            event_id="",
            version=EVENT_ENVELOPE_VERSION,
            seq=0,
            type="step_completed",
            job_id=base["job_id"],
            trace_id=base["trace_id"],
            payload=build_payload("step_completed", completed_values),
        )
    ]

    state = _STATUS_TO_CONTROL_STATE.get(status, "failed")
    if refs:
        for ref in refs:
            events.append(
                EventEnvelope(
                    version=EVENT_ENVELOPE_VERSION,
                    seq=0,
                    type="artifact_created",
                    job_id=base["job_id"],
                    trace_id=base["trace_id"],
                    payload=build_payload("artifact_created", ref),
                )
            )
    if status == "pending_approval":
        events.append(
            EventEnvelope(
                version=EVENT_ENVELOPE_VERSION,
                seq=0,
                type="approval_required",
                job_id=base["job_id"],
                trace_id=base["trace_id"],
                payload=build_payload("approval_required", {
                    "step_id": raw_step,
                    "capability": str(getattr(result, "namespace", "") or ""),
                    "action": tool_name,
                    "target": "",
                    "risk_level": str(view.get("risk_level") or ""),
                    "preview_ref": str(view.get("cursor") or ""),
                }),
            )
        )
    elif state == "failed":
        events.append(
            EventEnvelope(
                version=EVENT_ENVELOPE_VERSION,
                seq=0,
                type="error",
                job_id=base["job_id"],
                trace_id=base["trace_id"],
                payload=build_payload("error", ErrorPayload(
                    code=str(getattr(error, "code", "") or ""),
                    message=str(getattr(error, "message", "") or summary),
                    retryable=bool(getattr(result, "retryable", False)),
                    step_id=raw_step,
                    suggested_action=str(getattr(error, "suggested_action", "") or ""),
                ).model_dump(mode="json", exclude_none=True)),
            )
        )
    return events


def terminal_control_event(
    *,
    state: str,
    job_id: str = "",
    conversation_id: str = "",
    reason_code: str = "",
    reason: str = "",
    next_action: str = "",
) -> EventEnvelope:
    """任务级终态事件（``control``）：取代各调用点自造的错误/完成结构。"""
    return EventEnvelope(
        version=EVENT_ENVELOPE_VERSION,
        seq=0,
        type="control",
        job_id=str(job_id or ""),
        conversation_id=str(conversation_id or ""),
        payload=build_payload("control", ControlPayload(
            state=str(state or "completed"),
            reason_code=str(reason_code or ""),
            reason=sanitize_process_text(reason, limit=SUMMARY_MAX_CHARS),
            next_action=str(next_action or ""),
        ).model_dump(mode="json", exclude_none=True)),
    )


def step_completed_event(
    *,
    step_id: str,
    status: str = "completed",
    duration_ms: int = 0,
    output_summary: str = "",
    error_code: str = "",
    result_ref: dict[str, Any] | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    job_id: str = "",
) -> EventEnvelope:
    """步骤完成事件（供 Job/步骤快照出口复用同一形状）。"""
    return EventEnvelope(
        version=EVENT_ENVELOPE_VERSION,
        seq=0,
        type="step_completed",
        job_id=str(job_id or ""),
        payload=build_payload("step_completed", StepCompletedPayload(
            step_id=str(step_id or ""),
            status=str(status or "completed"),
            duration_ms=int(duration_ms or 0),
            output_summary=sanitize_process_text(output_summary, limit=SUMMARY_MAX_CHARS),
            error_code=str(error_code or ""),
            result_ref=dict(result_ref or {}),
            artifact_refs=list(artifact_refs or []),
        ).model_dump(mode="json", exclude_none=True)),
    )


__all__ = [
    "artifact_refs_of",
    "execution_result_events",
    "result_ref",
    "step_completed_event",
    "terminal_control_event",
    "ui_view",
]
