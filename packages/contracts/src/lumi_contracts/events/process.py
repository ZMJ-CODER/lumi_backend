"""执行过程日志契约（Agent 任务气泡的唯一数据形状）。

前端不解析自然语言、不猜"读取/编辑/Pwsh"：``kind`` 由后端给出，前端只渲染。
这些条目既是实时 SSE 的载荷，也是 ``JobRunView.process_log`` 的持久化内容，因此

* 只允许**安全摘要**：不落原始工具参数、原始响应、DSML/XML、绝对本地路径、令牌；
* 去重键明确：``entry_id`` 优先，其次 ``job_id + sequence``，工具条目用稳定 ``call_id``；
* 长度有界，避免把正文塞进过程日志。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

# ── 长度上限（过程日志是"过程"，不是正文）──
TITLE_MAX_CHARS = 120
SUMMARY_MAX_CHARS = 300
DETAIL_MAX_CHARS = 600


class ProcessKind(StrEnum):
    """过程条目的语义类别（由后端判定，前端不猜）。"""

    THINKING = "thinking"
    READ = "read"
    EDIT = "edit"
    COMMAND = "command"
    TOOL = "tool"
    SYSTEM = "system"


class ProcessStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING = "pending"


# 工具名 → kind（后端唯一判定处；前端不得再按名字猜）。
_READ_TOOLS = frozenset({
    "workspace_navigator", "workspace_read", "workspace_list", "workspace_search",
    "workspace_stat", "workspace_content_extract", "read_document", "inspect_document_set",
    "office_doc_read", "filestat", "read", "glob", "grep", "web_fetch", "web_search",
    "query_knowledge", "get_project_context",
})
_EDIT_TOOLS = frozenset({
    "workspace_stage_write", "workspace_stage_delete", "workspace_commit", "workspace_rollback",
    # 统一操作契约（OperationResult）：编辑/移动/删除都是工作区改动，
    # 过程日志必须把它们显示成"编辑"而不是泛化工具。
    "workspace_write", "workspace_edit", "workspace_move", "workspace_delete",
    "apply_patch", "edit", "write", "notebookedit", "delete", "rename",
    "office_doc_edit", "create_office_document", "install_new_dependencies",
    "rollback_dependency_manifests",
})
_COMMAND_TOOLS = frozenset({
    "bash", "bashoutput", "killshell", "run_in_sandbox", "sandbox_run", "sandbox_prepare",
    "sandbox_output_read", "sandbox_reset", "sandbox_commit", "python_exec", "git",
    "run_static_check", "check_new_dependencies", "curl", "processlist", "processsignal",
})

# 绝对本地路径（含盘符与常见宿主目录）：一律不进入过程日志。
_ABS_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"'，。；)]+|/(?:Users|home|var|tmp|opt|etc|root|mnt)/[^\s\"'，。；)]*)"
)
# 凭据形态：出现即替换（顺序：先具体前缀，再 key: value / Bearer 整段）。
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9._\-]{6,}"),
    re.compile(r"(?i)\bghp_[A-Za-z0-9]{6,}"),
    re.compile(
        r"(?i)\b(?:authorization|password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*\S+(?:\s+\S+)?"
    ),
    re.compile(r"(?i)\bbearer\s+\S+"),
)
# 原始协议/参数痕迹：整段丢弃。
_RAW_PAYLOAD_RE = re.compile(r"(?is)<\s*dsml|<\s*\|?\s*(?:tool_calls|invoke|parameter)\b|^\s*[\{\[]")
# 这些字段名属于"原始参数/响应"，永不进入过程日志。
_FORBIDDEN_KEYS = frozenset({
    "arguments", "args", "parameters", "result", "raw", "raw_result", "output", "response",
    "prompt", "messages", "reasoning", "thinking", "chain_of_thought", "content",
})


def sanitize_process_text(value: Any, *, limit: int) -> str:
    """把任意文本收敛成可展示的安全摘要（去绝对路径/凭据/协议痕迹）。"""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    text = _ABS_PATH_RE.sub("[路径]", text)
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub("[已隐藏]", text)
    if _RAW_PAYLOAD_RE.search(text):
        # 协议/原始载荷痕迹（DSML/XML/JSON 片段）：整条丢弃，只留占位符，
        # 不能"删掉前缀留下参数"，那等于还是把原始载荷展示给用户。
        return "[已隐藏]"
    return text[:limit]


def derive_kind(*, event_type: str = "", tool_name: str = "", explicit: str = "") -> ProcessKind:
    """按事件类型/工具名判定 ``kind``（显式声明优先）。"""
    for source in (explicit,):
        text = str(source or "").strip().casefold()
        for item in ProcessKind:
            if item.value == text:
                return item
    name = str(tool_name or "").strip().casefold()
    if name:
        if name in _READ_TOOLS:
            return ProcessKind.READ
        if name in _EDIT_TOOLS:
            return ProcessKind.EDIT
        if name in _COMMAND_TOOLS:
            return ProcessKind.COMMAND
        return ProcessKind.TOOL
    event = str(event_type or "").strip().casefold()
    if event in {"tool", "tool_started", "tool_completed"}:
        return ProcessKind.TOOL
    if event in {"error", "task_failed", "cancelled"}:
        return ProcessKind.SYSTEM
    return ProcessKind.THINKING


class ProcessLogEntry(BaseModel):
    """一条执行过程记录（安全摘要 + 去重键 + 状态）。"""

    id: str = ""
    entry_id: str = ""
    kind: ProcessKind = ProcessKind.THINKING
    title: str = ""
    summary: str = ""
    detail: str = ""
    status: ProcessStatus = ProcessStatus.RUNNING
    step_id: str = ""
    call_id: str = ""
    tool_name: str = ""
    sequence: int = 0
    occurred_at: str = ""
    job_id: str = ""

    @property
    def dedup_key(self) -> str:
        """去重键：entry_id 优先，其次 call_id，最后 job_id+sequence。"""
        if self.entry_id:
            return f"entry:{self.entry_id}"
        if self.call_id:
            return f"call:{self.job_id}:{self.call_id}"
        return f"seq:{self.job_id}:{self.sequence}"

    @classmethod
    def from_event(
        cls,
        event: Any,
        *,
        job_id: str = "",
        sequence: int = 0,
        occurred_at: str = "",
    ) -> "ProcessLogEntry":
        """从事件 dict/对象构造条目；只读取安全字段，原始参数一律不取。"""
        data = event if isinstance(event, dict) else {}
        # 自动（auto/step_confirm）路径把步骤信息放在 ``step`` 子对象里，
        # 内核事件用 ``result_summary`` / ``display`` 表达进度；这些都属于安全字段，
        # 必须一起读，否则实时帧会只有标题为空、摘要为空的过程行。
        step = data.get("step") if isinstance(data.get("step"), dict) else {}
        display = data.get("display") if isinstance(data.get("display"), dict) else {}
        tool_name = str(
            data.get("tool_name") or data.get("tool") or data.get("name") or step.get("tool") or ""
        )
        if not tool_name and isinstance(data.get("tool_call"), dict):
            tool_name = str((data["tool_call"].get("function") or {}).get("name") or "")
        event_type = str(data.get("type") or "")
        step_id_raw = str(data.get("step_id") or data.get("node_id") or step.get("id") or "")
        call_id = str(data.get("call_id") or (f"call-{step_id_raw}" if step_id_raw else ""))
        # 只取可展示字段：绝不读 arguments/params/result/output/prompt/reasoning。
        title_raw = data.get("title") or step.get("title") or ""
        summary_raw = (
            data.get("summary")
            or data.get("content")
            or data.get("message")
            or data.get("result_summary")
            or display.get("working")
            or display.get("completed")
            or ""
        )
        detail_raw = data.get("safe_detail") or data.get("detail") or display.get("completed") or ""
        status = str(
            data.get("status") or step.get("runtime_status") or step.get("status") or ""
        ).casefold()
        entry_status = ProcessStatus.RUNNING
        if status in {item.value for item in ProcessStatus}:
            entry_status = ProcessStatus(status)
        elif event_type.endswith("_completed") or event_type == "done":
            entry_status = ProcessStatus.COMPLETED
        elif event_type in {"task_failed", "error"}:
            entry_status = ProcessStatus.FAILED
        call_id = str(data.get("call_id") or "")
        # 稳定 entry_id：优先显式给；否则用 call_id（工具事件）或 job+sequence（过程事件），
        # 保证 SSE 重连/轮询/刷新同一条日志只出现一次。
        entry_id = str(data.get("entry_id") or data.get("id") or "")
        if not entry_id:
            entry_id = (
                f"call:{call_id}" if call_id else f"seq:{job_id or data.get('job_id') or ''}:{int(data.get('sequence') or sequence or 0)}"
            )
        return cls(
            id=entry_id,
            entry_id=entry_id,
            kind=derive_kind(
                event_type=event_type,
                tool_name=tool_name,
                explicit=str(data.get("kind") or ""),
            ),
            title=sanitize_process_text(title_raw, limit=TITLE_MAX_CHARS),
            summary=sanitize_process_text(summary_raw, limit=SUMMARY_MAX_CHARS),
            detail=sanitize_process_text(detail_raw, limit=DETAIL_MAX_CHARS),
            status=entry_status,
            step_id=step_id_raw,
            call_id=call_id,
            tool_name=tool_name[:80],
            sequence=int(data.get("sequence") or sequence or 0),
            occurred_at=str(data.get("occurred_at") or occurred_at or ""),
            job_id=str(data.get("job_id") or job_id or ""),
        )

    def to_sse_fields(self) -> dict[str, Any]:
        """SSE 出口用的扁平字段（前端直接消费，不需要再推导）。"""
        payload: dict[str, Any] = {
            "entry_id": self.entry_id or self.id,
            "kind": str(self.kind),
            "title": self.title,
            "summary": self.summary,
            "status": str(self.status),
            "sequence": int(self.sequence),
        }
        if self.detail:
            # 同时给 detail 与 safe_detail：前端读安全字段，旧字段保持兼容。
            payload["detail"] = self.detail
            payload["safe_detail"] = self.detail
        if self.step_id:
            payload["step_id"] = self.step_id
        if self.call_id:
            payload["call_id"] = self.call_id
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        if self.occurred_at:
            payload["occurred_at"] = self.occurred_at
        if self.job_id:
            payload["job_id"] = self.job_id
        return payload


def merge_process_log(
    existing: list[Any] | None,
    incoming: list[Any] | None,
    *,
    limit: int = 200,
) -> list[ProcessLogEntry]:
    """按去重键合并过程日志（SSE / 轮询 / 刷新叠加都不会重复）。"""
    merged: dict[str, ProcessLogEntry] = {}
    order: list[str] = []
    for source in (existing or [], incoming or []):
        for raw in source:
            entry = raw if isinstance(raw, ProcessLogEntry) else ProcessLogEntry.model_validate(raw)
            key = entry.dedup_key
            if key in merged:
                # 同一条目后到的事件补状态/详情（running → completed/failed）。
                previous = merged[key]
                merged[key] = previous.model_copy(
                    update={
                        "status": entry.status,
                        "detail": entry.detail or previous.detail,
                        "summary": entry.summary or previous.summary,
                        "title": entry.title or previous.title,
                    }
                )
                continue
            merged[key] = entry
            order.append(key)
    rows = [merged[key] for key in order]
    return rows[-limit:] if limit and len(rows) > limit else rows


__all__ = [
    "DETAIL_MAX_CHARS",
    "ProcessKind",
    "ProcessLogEntry",
    "ProcessStatus",
    "SUMMARY_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "derive_kind",
    "merge_process_log",
    "sanitize_process_text",
]
