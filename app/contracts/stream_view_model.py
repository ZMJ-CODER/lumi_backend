"""流式事件 → 前端状态模型（**参考实现**，用于双协议一致性与联调验收）。

这不是第二个事件系统，而是把"前端 ``StreamConsumer`` 必须做的事"用后端能测的方式
写下来（方案 §7.2 / §7.3 / §8 阶段 6）：

* **归一化**：``delta`` 与 ``text_delta``、``content`` 与 ``payload.content`` 都收敛到
  同一个字段，组件不再解析原始帧；
* **排序 + 去重**：按 ``seq`` 排序、按 ``event_id``（缺省 ``job_id+seq``）去重；
* **终态丢弃**：收到终态帧（``done`` / ``control`` 终态 / ``task_failed`` / ``cancelled``）
  后，后续非终态帧一律丢弃——与后端终态封印（``app.services.job_event_seal``）互为两端闸门；
* **未知事件安全降级**：记进 ``unsupported`` 而不是抛错/白屏。

``legacy`` 与 ``canonical`` 两种投影消费同一批内部事件后，:meth:`StreamViewModel.snapshot`
必须**逐字段一致**——这就是"切换协议不改前端行为"的机器可验证证据。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.job_event_seal import frame_state, is_seal_blocked, is_terminal_frame

#: 前端已知的事件类型（归一化后）。不属于这里的类型进 ``unsupported``。
_ANSWER_TYPES: frozenset[str] = frozenset({"text_delta", "delta", "text", "message", "content"})
_PROCESS_TYPES: frozenset[str] = frozenset({"process", "thinking", "thought"})
_STEP_STARTED_TYPES: frozenset[str] = frozenset({"step_started", "tool_started", "tool"})
_STEP_COMPLETED_TYPES: frozenset[str] = frozenset({"step_completed", "tool_completed", "step"})
_ARTIFACT_TYPES: frozenset[str] = frozenset({"artifact_created", "artifact"})
_VIEW_TYPES: frozenset[str] = frozenset({"view_updated", "view"})
_APPROVAL_REQUIRED_TYPES: frozenset[str] = frozenset({"approval_required"})
_APPROVAL_RESOLVED_TYPES: frozenset[str] = frozenset({"approval_resolved"})
_ERROR_TYPES: frozenset[str] = frozenset({"error", "task_failed"})
_METADATA_TYPES: frozenset[str] = frozenset({
    "job", "task_router", "title", "summary", "audio_ready", "usage", "warning",
    "plan_ready", "plan_delta", "waiting_next", "connected", "ping", "pong",
    "capability_requested", "waiting_provider", "provider_connected",
    "provider_disconnected", "capability_started", "capability_completed",
    "capability_failed", "plugin_health_changed",
    "operation_started", "operation_preview", "operation_completed",
    "operation_failed", "operation_rolled_back", "control", "done", "task_completed",
    "cancelled", "job_cancelled",
})


def _payload_of(frame: Mapping[str, Any]) -> dict[str, Any]:
    """取载荷：标准投影在 ``payload``，旧投影在顶层扁平字段。"""
    payload = frame.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _first(frame: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> Any:
    for source in (payload, frame):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _nested(frame: Mapping[str, Any], payload: Mapping[str, Any], *container_keys: str) -> dict[str, Any]:
    """取嵌套容器（旧投影里产物/视图可能整体挂在 ``artifact`` / ``view`` 下）。"""
    for source in (payload, frame):
        for key in container_keys:
            value = source.get(key)
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def _artifact_field(frame: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> Any:
    nested = _nested(frame, payload, "artifact", "artifact_ref", "artifact_refs", "view")
    for key in keys:
        value = nested.get(key)
        if value not in (None, ""):
            return value
    return _first(frame, payload, *keys)


def _view_field(frame: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> Any:
    nested = _nested(frame, payload, "view")
    for key in keys:
        value = nested.get(key)
        if value not in (None, ""):
            return value
    return _first(frame, payload, *keys)


def dedupe_key(frame: Mapping[str, Any]) -> str:
    """去重键：``event_id`` 优先，其次 ``job_id + seq``（与后端同一规则）。"""
    event_id = str(frame.get("event_id") or "")
    if event_id:
        return f"event:{event_id}"
    try:
        seq = int(frame.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return f"seq:{frame.get('job_id') or ''}:{seq}"


def normalize_frames(frames: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """前端归一化第一步：按 ``seq`` 排序 + 按去重键去重（保序、稳定）。"""
    indexed: list[tuple[int, int, dict[str, Any]]] = []
    for index, frame in enumerate(frames or []):
        if not isinstance(frame, Mapping):
            continue
        try:
            seq = int(frame.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        indexed.append((seq, index, dict(frame)))
    indexed.sort(key=lambda item: (item[0], item[1]))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for _seq, _index, frame in indexed:
        key = dedupe_key(frame)
        if key in seen:
            continue
        seen.add(key)
        out.append(frame)
    return out


@dataclass
class StreamViewModel:
    """前端状态模型（方案 §7.2 的机器可读版本）。"""

    job_id: str = ""
    conversation_id: str = ""
    status: str = ""
    answer_text: str = ""
    answer_format: str = "markdown"
    process_log: list[dict[str, Any]] = field(default_factory=list)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    views: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    dropped_after_terminal: int = 0
    last_seq: int = 0
    is_final: bool = False

    def snapshot(self) -> dict[str, Any]:
        """协议无关快照：两种投影消费后必须完全一致（阶段 6 对比的就是它）。"""
        return {
            "job_id": self.job_id,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "answer_text": self.answer_text,
            "answer_format": self.answer_format,
            "process_log": self.process_log,
            "steps": self.steps,
            "artifacts": self.artifacts,
            "views": self.views,
            "approvals": self.approvals,
            "errors": self.errors,
            "unsupported": sorted(set(self.unsupported)),
            "dropped_after_terminal": self.dropped_after_terminal,
            "is_final": self.is_final,
        }


def consume_frames(frames: Iterable[Mapping[str, Any]]) -> StreamViewModel:
    """消费一批帧 → :class:`StreamViewModel`（含排序、去重、终态丢弃、未知降级）。"""
    vm = StreamViewModel()
    for frame in normalize_frames(frames):
        event_type = str(frame.get("type") or "").strip()
        if not event_type:
            vm.unsupported.append("(missing type)")
            continue
        payload = _payload_of(frame)
        if vm.is_final and not is_terminal_frame(frame) and is_seal_blocked(frame):
            # §7.3 #3：终态之后到达的**内容类**事件直接丢弃（与后端封印对称）。
            # 只丢内容类而不是"一切非终态"：``task_failed`` 的兼容伴随帧 ``error``
            # 与标题/用量等元数据帧必须仍然生效，否则终态会把自己的伴随帧吃掉。
            vm.dropped_after_terminal += 1
            continue
        if not vm.job_id:
            vm.job_id = str(frame.get("job_id") or payload.get("job_id") or "")
        if not vm.conversation_id:
            vm.conversation_id = str(frame.get("conversation_id") or "")
        event_id = str(frame.get("event_id") or "")
        if event_id:
            vm.event_ids.append(event_id)
        try:
            vm.last_seq = max(vm.last_seq, int(frame.get("seq") or 0))
        except (TypeError, ValueError):
            pass

        if event_type in _ANSWER_TYPES:
            text = _first(frame, payload, "content", "text", "delta")
            if text:
                vm.answer_text += str(text)
            fmt = str(_first(frame, payload, "format") or "markdown")
            vm.answer_format = "plain" if fmt.strip().casefold() == "plain" else "markdown"
        elif event_type in _PROCESS_TYPES:
            vm.process_log.append({
                "entry_id": str(_first(frame, payload, "entry_id", "call_id") or ""),
                "kind": str(_first(frame, payload, "kind") or "thinking"),
                "title": str(_first(frame, payload, "title") or ""),
                "summary": str(_first(frame, payload, "summary", "detail") or ""),
                "status": str(_first(frame, payload, "status") or "running"),
            })
        elif event_type in _STEP_STARTED_TYPES:
            step_id = str(_first(frame, payload, "step_id", "call_id", "entry_id") or "")
            if step_id:
                vm.steps[step_id] = {
                    "step_id": step_id,
                    "status": "running",
                    "name": str(_first(frame, payload, "name", "title", "tool_name") or ""),
                    "summary": str(_first(frame, payload, "display_summary", "summary") or ""),
                    "duration_ms": 0,
                    "error_code": "",
                }
        elif event_type in _STEP_COMPLETED_TYPES:
            step_id = str(_first(frame, payload, "step_id", "call_id", "entry_id") or "")
            if step_id:
                entry = vm.steps.setdefault(step_id, {"step_id": step_id, "name": "", "summary": ""})
                entry["status"] = str(_first(frame, payload, "status") or "completed")
                entry["summary"] = str(
                    _first(frame, payload, "output_summary", "summary", "display_summary")
                    or entry.get("summary", "")
                )
                entry["duration_ms"] = int(_first(frame, payload, "duration_ms") or 0)
                entry["error_code"] = str(_first(frame, payload, "error_code") or "")
        elif event_type in _ARTIFACT_TYPES:
            artifact_id = str(_artifact_field(frame, payload, "artifact_id", "ref_id", "id") or "")
            if artifact_id:
                vm.artifacts[artifact_id] = {
                    "artifact_id": artifact_id,
                    "type": str(_artifact_field(frame, payload, "type", "schema_name", "kind") or ""),
                    "filename": str(_artifact_field(frame, payload, "filename", "name") or ""),
                    "mime_type": str(_artifact_field(frame, payload, "mime_type", "media_type") or ""),
                }
        elif event_type in _VIEW_TYPES:
            view_id = str(_view_field(frame, payload, "view_id", "id") or "")
            if view_id:
                vm.views[view_id] = {
                    "view_id": view_id,
                    "view_type": str(_view_field(frame, payload, "view_type", "type") or ""),
                    "action": str(_view_field(frame, payload, "action") or "upsert"),
                    "has_data": bool(_view_field(frame, payload, "data")),
                    "data_ref": str(_view_field(frame, payload, "data_ref") or ""),
                    "truncated": bool(_view_field(frame, payload, "truncated")),
                }
        elif event_type in _APPROVAL_REQUIRED_TYPES:
            request_id = str(_first(frame, payload, "request_id", "node_id", "step_id") or "")
            if request_id:
                vm.approvals[request_id] = {
                    "request_id": request_id,
                    "node_id": str(_first(frame, payload, "node_id") or ""),
                    "risk_level": str(_first(frame, payload, "risk_level") or ""),
                    "approved": None,
                }
        elif event_type in _APPROVAL_RESOLVED_TYPES:
            request_id = str(_first(frame, payload, "request_id", "node_id", "step_id") or "")
            if request_id:
                entry = vm.approvals.setdefault(request_id, {"request_id": request_id})
                entry["approved"] = bool(_first(frame, payload, "approved"))
        elif event_type in _ERROR_TYPES:
            from lumi_contracts.events.errors import translate_error

            # 统一错误模型：两种投影的 error 帧都要收敛成同一个 code + safe_message，
            # 否则"切换协议后错误文案变了"（旧投影给的是自由文本 + 旧错误码）。
            unified = translate_error({
                "code": _first(frame, payload, "code", "error_code", "reason_code") or "",
                "retryable": _first(frame, payload, "retryable"),
                "safe_message": _first(frame, payload, "safe_message") or "",
            })
            vm.errors.append({
                "code": unified.code,
                "category": unified.category,
                "safe_message": unified.safe_message,
                "retryable": unified.retryable,
            })
        elif event_type in _METADATA_TYPES:
            pass
        else:
            vm.unsupported.append(event_type)

        if is_terminal_frame(frame):
            state = frame_state(frame)
            vm.is_final = True
            if event_type in {"done", "task_completed"}:
                vm.status = state if state in {"failed", "cancelled", "interrupted"} else "completed"
            elif event_type in {"task_failed", "error"}:
                vm.status = "failed"
            elif event_type in {"cancelled", "job_cancelled"}:
                vm.status = "cancelled"
            elif event_type == "control":
                vm.status = state or "completed"
            elif not vm.status:
                vm.status = state or "completed"
    return vm


__all__ = [
    "StreamViewModel",
    "consume_frames",
    "dedupe_key",
    "normalize_frames",
]
