"""统一事件信封契约（过渡期：一套内部标准事件，两种 SSE 投影）。

设计约束（与方案一致，务必遵守）：

* **不新增第二套事件系统**：本模块只扩展既有 ``StreamEvent`` / ``ProcessLogEntry``
  / ``ExecutionResult`` / ``JobRunView``，不引入并行的事件总线或第二个 SSE 出口；
* **``payload`` 是唯一事件负载**：``data`` 只是旧版传输字段，由投影层从同一份
  标准事件生成，业务代码不维护两套字段；
* **``thought_delta`` 不传原始思维链**：过程类载荷只允许后端生成的安全摘要
  （``ProcessLogEntry`` 的安全字段），原始推理/工具参数/完整工具结果永不进入
  信封——这是**结构性约束**（``strip_unsafe_payload`` 在构造时执行），不是约定；
* **字段只有一套权威定义**：``event_id`` / ``version`` / ``seq`` / ``type`` /
  ``trace_id`` / ``conversation_id`` / ``job_id`` / ``occurred_at`` / ``payload``；
  沿用项目既有 ``job_id``，不新增 ``job_run_id``。

事件类型收敛为 11 个标准类型（``CanonicalEventType``）；未列入收敛表的既有类型
（``capability_*`` / ``operation_*`` / ``plan_ready`` / ``waiting_next`` / ``job`` …）
**原样透传**，因为前端契约要求"未知类型不得白屏"。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lumi_contracts.events.errors import translate_error
from lumi_contracts.events.lifecycle import TERMINAL_STATES
from lumi_contracts.execution.artifacts import ARTIFACT_RETENTION_FIELDS

# 标准信封版本：破坏性改名才升版本；新增可选字段不升版本。
#
# **必须与前端 `streamConsumer.STREAM_EVENT_VERSION`（当前 1）对齐**：前端对
# ``version > 支持的版本`` 的帧会走 `UNSUPPORTED_VERSION` 降级并**丢弃**，
# 因此这里不能凭空大于前端的支持版本（canonical 协议从 1 开始）。
EVENT_ENVELOPE_VERSION = 1

# 载荷文本长度上限（事件是"过程/展示"，不是正文通道）。
# 注意：``text_delta.content`` **不做展示级截断**——它就是正文本身，截断即丢字。
# 这里只留一个"失控增量"护栏（正常增量在几十~几千字符量级）。
TEXT_DELTA_RUNAWAY_GUARD = 1_000_000
SUMMARY_MAX_CHARS = 300
DETAIL_MAX_CHARS = 600
TITLE_MAX_CHARS = 120


class CanonicalEventType(StrEnum):
    """收敛后的标准事件类型（前端只按这些类型分派新协议）。"""

    TEXT_DELTA = "text_delta"
    PROCESS = "process"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    ARTIFACT_CREATED = "artifact_created"
    VIEW_UPDATED = "view_updated"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    CONTROL = "control"
    DONE = "done"
    ERROR = "error"


CANONICAL_EVENT_TYPES: frozenset[str] = frozenset(item.value for item in CanonicalEventType)

#: 载荷结构版本（Schema 注册表的注册维度之一，见 ``events/registry.py``）。
#:
#: 规则：同一 ``type`` 内**只允许加字段**（additive），因此只加字段不升版本；
#: 破坏性改结构要**新建 type**，所以这里每个 type 只有 v1。
PAYLOAD_SCHEMA_VERSIONS: dict[str, int] = {
    event_type: 1 for event_type in sorted(CANONICAL_EVENT_TYPES)
}

#: 视图数据上限（方案 §2.3）：超出即不再通过事件搬运正文，改为 ``data_ref``。
#: 注意与 ``plugins.view_contribution.VIEW_DATA_MAX_BYTES``（400KB，插件契约上限）
#: 的区别：**事件通道**更严（64KB），因为事件是实时通道而不是数据搬运通道。
VIEW_EVENT_DATA_MAX_BYTES = 65_536
VIEW_EVENT_DATA_MAX_DEPTH = 10
VIEW_EVENT_DATA_MAX_ITEMS = 1_000

#: ``control`` 出现这些状态即为**任务定局**（终态封印的判据）。
#: ``waiting_clarification`` / ``waiting_approval`` 是"等外部输入"，不是定局。
TERMINAL_CONTROL_STATES: frozenset[str] = frozenset(
    {state.value for state in TERMINAL_STATES} | {"blocked", "cancelled"}
)


def is_terminal_state(state: Any) -> bool:
    """``control.state`` 是否定局（终态）。"""
    return str(state or "").strip().casefold() in TERMINAL_CONTROL_STATES


def is_terminal_envelope(event_type: Any, payload: Any = None) -> bool:
    """事件是否为**终态帧**（``done`` 或 ``control`` 的终态）——封印判据唯一实现。"""
    name = str(event_type or "").strip()
    if name == CanonicalEventType.DONE.value:
        return True
    if name != CanonicalEventType.CONTROL.value:
        return False
    values = payload if isinstance(payload, dict) else {}
    return is_terminal_state(values.get("state"))

#: 旧事件类型 → 标准事件类型（唯一登记处；适配器只查这张表，不再各自 if/else）。
#:
#: ``step`` 是**双态**旧类型（同一步骤先 running 后 completed），因此它不在这张
#: 静态表里，由 :func:`canonical_event_type` 按 ``status`` 判定。
LEGACY_EVENT_ALIASES: dict[str, str] = {
    "delta": CanonicalEventType.TEXT_DELTA.value,
    "text": CanonicalEventType.TEXT_DELTA.value,
    "message": CanonicalEventType.TEXT_DELTA.value,
    "process": CanonicalEventType.PROCESS.value,
    "thinking": CanonicalEventType.PROCESS.value,
    "tool_started": CanonicalEventType.STEP_STARTED.value,
    "step_started": CanonicalEventType.STEP_STARTED.value,
    "tool_completed": CanonicalEventType.STEP_COMPLETED.value,
    "step_completed": CanonicalEventType.STEP_COMPLETED.value,
    "tool": CanonicalEventType.STEP_STARTED.value,
    "artifact_created": CanonicalEventType.ARTIFACT_CREATED.value,
    "artifact": CanonicalEventType.ARTIFACT_CREATED.value,
    "view_updated": CanonicalEventType.VIEW_UPDATED.value,
    "approval_required": CanonicalEventType.APPROVAL_REQUIRED.value,
    "approval_resolved": CanonicalEventType.APPROVAL_RESOLVED.value,
    # ``done`` → ``control(state=completed)``；旧版 ``done`` 由旧投影继续输出。
    "done": CanonicalEventType.CONTROL.value,
    "task_completed": CanonicalEventType.CONTROL.value,
    # ``task_failed`` → ``control(state=failed)``；旧版错误帧由旧投影继续输出。
    "task_failed": CanonicalEventType.CONTROL.value,
    # 取消：``control(state=cancelled)`` + 兼容 ``done``（旧前端收到即停止流式）。
    "cancelled": CanonicalEventType.CONTROL.value,
    "job_cancelled": CanonicalEventType.CONTROL.value,
    "error": CanonicalEventType.ERROR.value,
}

#: 旧类型 → 标准 ``control.state``（只对映射到 control 的类型有意义）。
#: 任务终态在标准协议里是 ``control``，旧 ``done`` / ``task_failed`` 通过
#: :func:`canonical_events_for` 作为**兼容伴随帧**继续输出。
_TERMINAL_CONTROL_STATE: dict[str, str] = {
    "done": "completed",
    "task_completed": "completed",
    "task_failed": "failed",
    "cancelled": "cancelled",
    "job_cancelled": "cancelled",
}

#: 这些旧类型必须**先查别名表**（即使名字本身也是标准类型）：
#: ``done`` 在收敛表里既是标准类型、又语义上等价于 ``control(completed)``。
_COMPAT_ALIAS_FIRST: frozenset[str] = frozenset(_TERMINAL_CONTROL_STATE)

#: 终态事件必须补发的兼容伴随帧（``control`` + 旧类型名）。
COMPAT_COMPANION_TYPES: dict[str, str] = {
    "done": "done",
    "task_completed": "done",
    "task_failed": "error",
    # 取消的兼容伴随帧是 ``done``：旧前端以 ``done`` 作为"流结束"信号。
    "cancelled": "done",
    "job_cancelled": "done",
}

#: ``step`` 双态旧类型的状态判定。
_STEP_STARTED_STATUSES = frozenset({"", "pending", "ready", "planned", "running", "retrying", "in_progress"})
_STEP_COMPLETED_STATUSES = frozenset({"completed", "succeeded", "success", "failed", "error", "cancelled", "interrupted", "skipped"})


def canonical_event_type(event_type: str, *, status: str = "") -> str:
    """旧/新事件类型 → 标准事件类型；未登记的既有类型原样返回（前端容忍未知类型）。"""
    text = str(event_type or "").strip()
    if not text:
        return CanonicalEventType.ERROR.value
    if text in _COMPAT_ALIAS_FIRST:
        # done/task_failed 的标准形态是 control(...)，不是同名类型。
        return LEGACY_EVENT_ALIASES[text]
    if text in CANONICAL_EVENT_TYPES:
        return text
    if text == "step":
        key = str(status or "").strip().casefold()
        if key in _STEP_COMPLETED_STATUSES:
            return CanonicalEventType.STEP_COMPLETED.value
        if key in _STEP_STARTED_STATUSES:
            return CanonicalEventType.STEP_STARTED.value
        return CanonicalEventType.STEP_STARTED.value
    return LEGACY_EVENT_ALIASES.get(text, text)


def is_canonical_event_type(event_type: str) -> bool:
    return str(event_type or "").strip() in CANONICAL_EVENT_TYPES


# ── 安全载荷策略：原始思维链/参数/结果永远进不了信封 ──────────────

#: 永远不允许出现在标准事件载荷里的键（大小写不敏感、按后缀匹配）。
#: **任何**公开投影都不允许出现的键（标准投影与旧投影共用这道边界）：
#: 原始思维链、工具参数、提示词、凭据、堆栈、供应商原文。
SECRET_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "arguments", "args", "parameters", "params", "prompt", "prompts", "messages",
    "reasoning", "reasoning_content", "thinking", "chain_of_thought", "cot",
    "thought", "thoughts", "thought_delta", "system_prompt",
    "api_key", "token", "access_token", "refresh_token", "password", "secret",
    "raw", "raw_result", "raw_output", "tool_result", "content_raw",
    "stack", "stack_trace", "traceback", "error_stack", "exception_text",
    "raw_request", "raw_response", "provider_response", "request_body", "response_body",
    "authorization", "cookie", "cookies", "credential", "credentials",
    "private_key", "secret_key", "session_token",
})

#: 永远不允许出现在标准事件载荷里的键 = 上面的机密集 + "原始结果/正文"家族。
#: 说明：``output`` / ``result`` / ``response`` 这些**正文类**键在标准投影里由
#: 载荷白名单统一裁掉（只留 ``output_summary``）；旧投影为了兼容既有前端仍会
#: 原样保留它们（例如 process 帧的 ``step.output``），但机密集在所有投影都被删。
FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = SECRET_PAYLOAD_KEYS | frozenset({
    "result", "response", "output",
})

#: 允许出现在载荷里的键（白名单；未登记的键在构造时被丢弃）。
#: 说明：这里只放"展示/关联/状态"字段，业务正文一律走 ``output_summary`` 摘要。
ALLOWED_PAYLOAD_KEYS: frozenset[str] = frozenset({
    # 通用关联
    "step_id", "call_id", "tool_name", "request_id", "message_id", "entry_id",
    "node_id",
    # 文本增量
    "content", "format",
    # 过程条目（ProcessLogEntry 安全字段）
    "kind", "title", "summary", "detail", "safe_detail", "status", "sequence", "occurred_at",
    # 步骤
    "step_type", "name", "display_summary", "duration_ms", "output_summary",
    "error_code", "result_ref", "artifact_refs",
    # 步骤展示子对象（``step`` / ``display`` 只保留白名单内的展示字段）
    "step", "display",
    # 产物
    "artifact_id", "type", "filename", "mime_type", "size_bytes", "expires_at",
    # 产物保留策略（与契约 ``ARTIFACT_RETENTION_FIELDS`` 同源；前端据此提示
    # "该产物受当前存储策略限制，将于 N 天后过期"）
    *ARTIFACT_RETENTION_FIELDS,
    "retention_clamped", "days_until_expiry", "retention_notice",
    # 视图
    "view_id", "view_type", "plugin_id", "plugin_version", "action", "schema_version",
    "data", "data_ref",
    # 审批
    "capability", "target", "risk_level", "preview_ref",
    # 控制/错误
    "state", "reason_code", "reason", "next_action", "code", "message",
    "retryable", "suggested_action", "approved", "resolved_by", "decided_at",
    # 统一错误模型（UnifiedError）：前端只展示 safe_message；detail_ref 是受权限
    # 保护的产物引用；category/retryable 决定前端默认动作。
    "category", "safe_message", "detail_ref", "safe_next_action",
    # 未知事件降级标记（前端据此记录"客户端不支持该事件"而不是白屏）
    "unsupported",
    # 路由审计（枚举值，非自由文本）
    "route_mode", "complexity", "safety_action",
    # 路由元数据事件（`task_router`）与任务快照展示字段：审计/恢复所需，
    # 不含用户原文与正文（`task_profile` / `run_view` / `steps` 走不透明容器）。
    "route_reason_code", "assessor_source", "policy_version", "execution_policy",
    "fallback_action", "job_status", "execution_mode", "plan_revision",
    "plan_text", "current_step_index", "dsml_pending", "task_completed", "task_failed",
    "completed_step_id", "next_step_id", "next_step_index", "button_label", "truncated",
    # 结构化展示容器（形状由各自契约定义 → 走不透明容器处理）
    "task_profile", "run_view", "steps",
    # 既有过程/能力/操作帧的展示字段（透传类型也要能安全投影）
    "provider_id", "health_status", "logical_path", "target_path", "operation",
    "execution_state", "has_more", "cursor",
    "effect_status", "candidates", "availability_hint",
})


#: "不透明但需保留"的容器字段：它们的**内部键名由各自契约定义**（视图数据、
#: 结果引用、产物引用、运行视图/步骤/任务画像），因此只做"危险键递归清除 +
#: 体积上限"，不再套顶层白名单。
OPAQUE_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "data", "result_ref", "artifact_refs", "run_view", "steps", "task_profile",
})

#: 不透明容器的体积上限（序列化后字节数）；超限直接丢弃，避免用事件搬运正文。
OPAQUE_VALUE_MAX_BYTES = 400_000


def _scrub_forbidden(value: Any, keys: frozenset[str] = FORBIDDEN_PAYLOAD_KEYS) -> Any:
    """递归删除危险键，但**保留**其余键名（用于不透明容器）。"""
    if isinstance(value, dict):
        return {
            str(key): _scrub_forbidden(item, keys)
            for key, item in value.items()
            if str(key).strip().casefold() not in keys
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_forbidden(item, keys) for item in value]
    return value


def _opaque_value(value: Any) -> Any:
    scrubbed = _scrub_forbidden(value)
    try:
        encoded = json.dumps(scrubbed, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {}
    if len(encoded.encode("utf-8")) > OPAQUE_VALUE_MAX_BYTES:
        return {}
    return scrubbed


def scrub_forbidden_keys(value: Any) -> Any:
    """递归删除机密集危险键但**保留其余键名**（旧投影的无损脱敏）。

    旧版 SSE 帧是"无损透传"的（前端当前按扁平字段消费），不能套载荷白名单；
    但原始思维链/工具参数/提示词/凭据/堆栈/供应商原文在**任何**公开投影里都不允许
    出现，因此旧投影在出口处过 :data:`SECRET_PAYLOAD_KEYS` 这一道。
    """
    return _scrub_forbidden(value, SECRET_PAYLOAD_KEYS)


def bounded_opaque_value(value: Any) -> Any:
    """不透明容器的安全收敛：递归清危险键 + 体积上限，但保留容器自己的键名。

    用于 ``data``（声明式视图数据）/ ``result_ref`` / ``artifact_refs`` 这类
    **形状由各自契约定义**的字段——它们的键名不该被事件层白名单裁掉。
    """
    return _opaque_value(value)


def _view_data_depth(value: Any, current: int = 1) -> int:
    if isinstance(value, dict):
        children = list(value.values())
    elif isinstance(value, (list, tuple)):
        children = list(value)
    else:
        return current
    if not children:
        return current
    return max(_view_data_depth(item, current + 1) for item in children)


def _view_data_items(value: Any) -> int:
    if isinstance(value, dict):
        return len(value) + sum(_view_data_items(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return len(value) + sum(_view_data_items(item) for item in value)
    return 0


def view_data_within_bounds(data: Any) -> bool:
    """视图 ``data`` 是否在体积/嵌套/元素上限内（超出 → 走 ``data_ref``）。"""
    try:
        size = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return False
    if size > VIEW_EVENT_DATA_MAX_BYTES:
        return False
    if _view_data_depth(data) > VIEW_EVENT_DATA_MAX_DEPTH:
        return False
    return _view_data_items(data) <= VIEW_EVENT_DATA_MAX_ITEMS


def _bound_view_mapping(mapping: dict[str, Any], view_id: Any) -> dict[str, Any]:
    data = mapping.get("data")
    if isinstance(data, dict) and data and not view_data_within_bounds(data):
        updated = dict(mapping)
        updated["data"] = {}
        updated["data_ref"] = str(mapping.get("data_ref") or f"view:{view_id or 'current'}")
        updated["truncated"] = True
        return updated
    return mapping


def bound_view_frame(values: Any) -> dict[str, Any]:
    """视图帧的通道级上限（**两种投影共用**）：超限只留 ``data_ref`` + ``truncated``。

    兼容两种形状：标准投影 ``data`` 在顶层，旧投影把整个视图放在 ``view`` 下。
    实现在契约层，保证"旧投影也不会把 100KB 视图数据塞进事件流"。
    """
    if not isinstance(values, dict):
        return {}
    out = dict(values)
    nested = out.get("view")
    if isinstance(nested, dict):
        out["view"] = _bound_view_mapping(nested, nested.get("view_id"))
    if "view_id" in out or "view_type" in out:
        out = _bound_view_mapping(out, out.get("view_id"))
    return out


def strip_unsafe_payload(payload: Any, *, allowed: frozenset[str] | None = None) -> dict[str, Any]:
    """把任意字典收敛为可上线的安全载荷（白名单 + 危险键兜底删除）。

    结构性保证：即使上游误把原始思维链/工具参数塞进事件，也进不了 SSE。
    不透明容器（``data`` / ``result_ref`` / ``artifact_refs``）只清危险键并限体积。
    """
    if not isinstance(payload, dict):
        return {}
    white = allowed if allowed is not None else ALLOWED_PAYLOAD_KEYS
    out: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key)
        lowered = name.strip().casefold()
        if lowered in FORBIDDEN_PAYLOAD_KEYS:
            continue
        if name not in white:
            continue
        if lowered in OPAQUE_PAYLOAD_KEYS:
            out[name] = _opaque_value(value)
        elif isinstance(value, dict):
            out[name] = strip_unsafe_payload(value)
        elif isinstance(value, (list, tuple)):
            out[name] = [
                strip_unsafe_payload(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            out[name] = value
    return out


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event_id(*parts: Any) -> str:
    """事件去重标识。

    给了稳定组成（``job_id`` + ``entry_id``/``call_id``/``seq``）时用内容哈希，
    因此 SSE 重连、快照恢复、重复发射的**同一逻辑事件**得到同一个 ``event_id``，
    前端可以直接去重；没有稳定组成时退回随机 id（只保证唯一）。
    """
    tokens = [str(part or "").strip() for part in parts]
    if all(tokens) and any(tokens):
        digest = hashlib.sha256("|".join(tokens).encode("utf-8")).hexdigest()
        return f"evt_{digest[:24]}"
    return f"evt_{uuid.uuid4().hex}"


# ── 各标准事件的载荷模型（唯一权威定义）──────────────────────────


class EventPayload(BaseModel):
    """载荷基类：忽略未知字段（新增键不需要前端同步发布）。"""

    model_config = ConfigDict(extra="ignore")


class TextDeltaPayload(EventPayload):
    """模型正文增量。``format`` 区分 Markdown 与纯文本，正文原样保留。"""

    content: str = ""
    format: str = "markdown"  # markdown | plain
    message_id: str = ""

    def model_post_init(self, __context: Any) -> None:  # noqa: D105 - 只有护栏与枚举收敛
        if len(self.content) > TEXT_DELTA_RUNAWAY_GUARD:
            object.__setattr__(self, "content", self.content[:TEXT_DELTA_RUNAWAY_GUARD])
        if self.format not in {"markdown", "plain"}:
            object.__setattr__(self, "format", "plain")


class ProcessPayload(EventPayload):
    """过程条目（安全摘要）：直接复用 ``ProcessLogEntry`` 的安全字段。"""

    entry_id: str = ""
    kind: str = "thinking"
    title: str = ""
    summary: str = ""
    detail: str = ""
    status: str = "running"
    step_id: str = ""
    call_id: str = ""
    tool_name: str = ""
    sequence: int = 0

    @classmethod
    def from_entry(cls, entry: Any) -> "ProcessPayload":
        """从 ``ProcessLogEntry`` 构造（唯一来源，避免二次推导文案）。"""
        return cls(
            entry_id=str(getattr(entry, "entry_id", "") or getattr(entry, "id", "")),
            kind=str(getattr(entry, "kind", "") or "thinking"),
            title=_clip(getattr(entry, "title", ""), TITLE_MAX_CHARS),
            summary=_clip(getattr(entry, "summary", ""), SUMMARY_MAX_CHARS),
            detail=_clip(getattr(entry, "detail", ""), DETAIL_MAX_CHARS),
            status=str(getattr(entry, "status", "") or "running"),
            step_id=str(getattr(entry, "step_id", "") or ""),
            call_id=str(getattr(entry, "call_id", "") or ""),
            tool_name=str(getattr(entry, "tool_name", "") or ""),
            sequence=int(getattr(entry, "sequence", 0) or 0),
        )


class StepStartedPayload(EventPayload):
    """步骤开始：只给脱敏展示摘要，不给完整 inputs。"""

    step_id: str = ""
    step_type: str = ""
    name: str = ""
    display_summary: str = ""
    tool_name: str = ""


class StepCompletedPayload(EventPayload):
    """步骤完成：完整结果只在 ``result_ref``，事件里只有摘要与引用。"""

    step_id: str = ""
    status: str = "completed"
    duration_ms: int = 0
    output_summary: str = ""
    error_code: str = ""
    result_ref: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)


class ArtifactCreatedPayload(EventPayload):
    """产物创建：只给引用与元数据，下载地址/令牌在受权限保护的下载接口。

    保留策略字段与 ``ARTIFACT_RETENTION_FIELDS`` 同名：``expires_at`` 是**生效**到期
    时间，另给请求值与夹取原因，前端不必再猜"为什么我的产物 7 天就没了"。
    """

    artifact_id: str = ""
    type: str = ""
    filename: str = ""
    mime_type: str = ""
    size_bytes: int | None = None
    expires_at: str = ""
    retention_class: str = ""
    requested_expires_at: str = ""
    effective_expires_at: str = ""
    retention_policy_source: str = ""
    retention_clamp_reason: str = ""
    retention_clamped: bool = False
    days_until_expiry: int = 0
    retention_notice: str = ""


class ViewUpdatedPayload(EventPayload):
    """声明式视图更新（第一阶段只支持白名单插件视图，不开 iframe/HTML/JS）。

    ``view_type`` 复用既有 ``ViewContribution`` 白名单词表（table/chart/diff/
    timeline/file_preview/form）；未知类型一律置空，前端显示"暂不支持此展示类型"。

    体积/嵌套上限（方案 §2.3，防 JSON 炸弹）：``data`` 超过
    :data:`VIEW_EVENT_DATA_MAX_BYTES` 字节、嵌套超过 :data:`VIEW_EVENT_DATA_MAX_DEPTH`
    层或元素超过 :data:`VIEW_EVENT_DATA_MAX_ITEMS` 个时，**不再通过事件搬运正文**，
    改为只发 ``data_ref``（前端按需拉取）。
    """

    view_id: str = ""
    view_type: str = ""
    plugin_id: str = ""
    plugin_version: str = ""
    action: str = "upsert"
    schema_version: int = 1
    title: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    data_ref: str = ""
    truncated: bool = False

    def model_post_init(self, __context: Any) -> None:  # noqa: D105 - 白名单收敛 + 体积上限
        from lumi_contracts.plugins.view_contribution import VIEW_TYPES

        if str(self.view_type or "").strip().casefold() not in VIEW_TYPES:
            # 未知/非白名单视图类型：类型置空**并且不下发数据**（纵深防御——
            # 类型都被拒了，数据就不该跟着出门；前端显示"暂不支持此展示类型"）。
            object.__setattr__(self, "view_type", "")
            object.__setattr__(self, "data", {})
            return
        if self.data and not view_data_within_bounds(self.data):
            object.__setattr__(self, "data", {})
            if not self.data_ref:
                object.__setattr__(self, "data_ref", f"view:{self.view_id or 'current'}")
            object.__setattr__(self, "truncated", True)


class ApprovalRequiredPayload(EventPayload):
    """等待审批：审批结果仍走既有审批 REST 接口回传。

    ``node_id`` 是**既有审批接口的主键**（``POST /agents/jobs/{job_id}/approve``
    的 ``node_id``）；``step_id`` 是展示步骤 id。两者都给，前端不必再猜。
    """

    request_id: str = ""
    node_id: str = ""
    step_id: str = ""
    capability: str = ""
    action: str = ""
    target: str = ""
    risk_level: str = ""
    preview_ref: str = ""
    expires_at: str = ""


class ApprovalResolvedPayload(EventPayload):
    request_id: str = ""
    step_id: str = ""
    approved: bool = False
    resolved_by: str = ""
    reason: str = ""
    decided_at: str = ""


class ControlPayload(EventPayload):
    """任务级控制信号（完成/失败/取消/暂停），取代各调用点自造错误结构。

    ``error_code`` 来自统一错误模型（失败/阻断时）；``safe_next_action`` 是给前端
    展示的"下一步动作"文案（``next_action`` 保留为兼容字段）。
    """

    state: str = "running"
    error_code: str = ""
    reason_code: str = ""
    reason: str = ""
    next_action: str = ""
    safe_next_action: str = ""

    def model_post_init(self, __context: Any) -> None:  # noqa: D105 - 错误码收敛
        if self.error_code:
            from lumi_contracts.events.errors import spec_for

            spec = spec_for(self.error_code)
            object.__setattr__(self, "error_code", spec.code)
            if not self.safe_next_action:
                object.__setattr__(self, "safe_next_action", spec.next_action)

    @property
    def terminal(self) -> bool:
        return is_terminal_state(self.state)


class ErrorPayload(EventPayload):
    """统一错误载荷（``UnifiedError`` 的事件形态，方案 §3）。

    **只有** ``safe_message`` 会到前端：原始异常文本、供应商响应、堆栈、工具参数
    都不在白名单里，构造时即被丢弃（``detail_ref`` 指向受权限保护的完整细节）。
    """

    code: str = "system.internal"
    category: str = ""
    retryable: bool | None = None
    safe_message: str = ""
    detail_ref: str = ""
    step_id: str = ""
    suggested_action: str = ""
    #: 前端（`streamErrors.normalizeUnifiedError`）读的是 ``safe_next_action``；
    #: ``suggested_action`` 是既有错误信封字段，两者同值，避免前端做字段猜测。
    safe_next_action: str = ""

    def model_post_init(self, __context: Any) -> None:  # noqa: D105 - 错误码收敛
        from lumi_contracts.events.errors import spec_for

        spec = spec_for(self.code)
        object.__setattr__(self, "code", spec.code)
        if self.retryable is None:
            object.__setattr__(self, "retryable", bool(spec.retryable))
        if not self.safe_message:
            object.__setattr__(self, "safe_message", spec.safe_message)
        if not self.category:
            object.__setattr__(self, "category", spec.category.value)
        if not self.suggested_action:
            object.__setattr__(self, "suggested_action", spec.next_action)
        if not self.safe_next_action:
            object.__setattr__(self, "safe_next_action", self.suggested_action)

    @classmethod
    def from_error(cls, error: Any, *, step_id: str = "", detail_ref: str = "") -> "ErrorPayload":
        """由任意错误来源构造（唯一入口：``translate_error``）。"""
        unified = translate_error(error, step_id=step_id, detail_ref=detail_ref)
        return cls(**unified.to_payload())


PAYLOAD_MODELS: dict[str, type[EventPayload]] = {
    CanonicalEventType.TEXT_DELTA.value: TextDeltaPayload,
    CanonicalEventType.PROCESS.value: ProcessPayload,
    CanonicalEventType.STEP_STARTED.value: StepStartedPayload,
    CanonicalEventType.STEP_COMPLETED.value: StepCompletedPayload,
    CanonicalEventType.ARTIFACT_CREATED.value: ArtifactCreatedPayload,
    CanonicalEventType.VIEW_UPDATED.value: ViewUpdatedPayload,
    CanonicalEventType.APPROVAL_REQUIRED.value: ApprovalRequiredPayload,
    CanonicalEventType.APPROVAL_RESOLVED.value: ApprovalResolvedPayload,
    CanonicalEventType.CONTROL.value: ControlPayload,
    CanonicalEventType.DONE.value: ControlPayload,
    CanonicalEventType.ERROR.value: ErrorPayload,
}


def payload_model(event_type: str) -> type[EventPayload] | None:
    return PAYLOAD_MODELS.get(str(event_type or "").strip())


def build_payload(event_type: str, values: Any) -> dict[str, Any]:
    """按类型构造并校验载荷；未登记的透传类型只做安全收敛（不猜形状）。"""
    safe = strip_unsafe_payload(values)
    model = payload_model(event_type)
    if model is None:
        return safe
    try:
        # 白名单已收敛一次；模型再按自己的字段定义做第二次裁剪与定长。
        return model.model_validate(safe).model_dump(mode="json", exclude_none=True)
    except Exception:  # noqa: BLE001 - 契约异常不得让事件流中断
        return safe


class EventEnvelope(BaseModel):
    """标准事件信封：字段只有这一套权威定义。"""

    model_config = ConfigDict(extra="ignore")

    event_id: str = ""
    version: int = EVENT_ENVELOPE_VERSION
    #: 载荷结构版本（Schema 注册表的注册维度；只加字段不升版本）。
    schema_version: int = 1
    seq: int = 0
    type: str = CanonicalEventType.ERROR.value
    trace_id: str = ""
    conversation_id: str = ""
    job_id: str = ""
    occurred_at: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        """是否终态帧（``done`` 或 ``control`` 的终态）——终态封印判据。"""
        return is_terminal_envelope(self.type, self.payload)

    @property
    def dedup_key(self) -> str:
        """去重键：``event_id`` 优先，其次 ``job_id + seq``（与旧过程日志同规则）。"""
        if self.event_id:
            return f"event:{self.event_id}"
        return f"seq:{self.job_id}:{int(self.seq)}"

    def with_seq(self, seq: int) -> "EventEnvelope":
        """由**编码器**分配流内序号（工厂不持有流计数器）。

        带稳定身份（``entry_id``/``call_id``/``step_id``）的事件在构造时已经拿到
        内容哈希 ``event_id``，重连/快照恢复后的同一逻辑事件仍然同 id；
        其余事件在这里补一个唯一 id。
        """
        return self.model_copy(update={
            "seq": int(seq),
            "event_id": self.event_id or make_event_id(),
        })

    def to_canonical_frame(self) -> dict[str, Any]:
        """标准 SSE 帧：``payload`` 是唯一负载，不再输出旧的扁平 ``data``。"""
        return {
            "event_id": self.event_id,
            "version": int(self.version),
            "schema_version": int(self.schema_version),
            "seq": int(self.seq),
            "type": str(self.type),
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "job_id": self.job_id,
            "occurred_at": self.occurred_at or _now_iso(),
            "payload": dict(self.payload),
        }


def build_envelope(
    event_type: str,
    payload: Any = None,
    *,
    seq: int = 0,
    job_id: str = "",
    conversation_id: str = "",
    trace_id: str = "",
    occurred_at: str = "",
    event_id: str = "",
    status: str = "",
) -> EventEnvelope:
    """标准事件工厂：一处决定类型收敛、载荷白名单、``event_id`` 与时间戳。"""
    values = dict(payload) if isinstance(payload, dict) else {}
    # 双态旧类型（``step``）靠状态判定方向；调用方没显式给就按载荷读。
    effective_status = str(
        status or values.get("status") or values.get("runtime_status") or ""
    )
    canonical = canonical_event_type(event_type, status=effective_status)
    if canonical in {CanonicalEventType.CONTROL.value, CanonicalEventType.DONE.value}:
        state = str(
            values.get("state")
            or _TERMINAL_CONTROL_STATE.get(str(event_type or ""), "")
            or "running"
        )
        values["state"] = state
    # 稳定身份优先（SSE 重连 / 快照恢复后同一条日志仍然同 id）。
    stable = str(
        values.get("entry_id") or values.get("call_id") or values.get("step_id") or ""
    )
    resolved_id = event_id or (make_event_id(job_id, canonical, stable) if stable else "")
    return EventEnvelope(
        event_id=resolved_id,
        version=EVENT_ENVELOPE_VERSION,
        schema_version=int(PAYLOAD_SCHEMA_VERSIONS.get(canonical, 1)),
        seq=int(seq or 0),
        type=canonical,
        trace_id=str(trace_id or ""),
        conversation_id=str(conversation_id or ""),
        job_id=str(job_id or ""),
        occurred_at=str(occurred_at or _now_iso()),
        payload=build_payload(canonical, values),
    )


def canonical_events_for(
    event_type: str,
    payload: Any = None,
    *,
    job_id: str = "",
    conversation_id: str = "",
    trace_id: str = "",
    occurred_at: str = "",
    status: str = "",
) -> list[EventEnvelope]:
    """一个旧事件 → **一组**标准事件（含终态兼容伴随帧）。

    收敛表里的"``done`` → ``control(state=completed)`` + 兼容 ``done``"就落在这里：
    ``done`` / ``task_completed`` 产出 ``control`` + ``done``，``task_failed`` 产出
    ``control`` + ``error``；其余类型一一对应。``seq`` 由编码器统一分配
    （:meth:`EventEnvelope.with_seq`），因此这里返回的帧序号为 0。
    """
    values = dict(payload) if isinstance(payload, dict) else {}
    primary = build_envelope(
        event_type,
        values,
        job_id=job_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
        occurred_at=occurred_at,
        status=status,
    )
    out = [primary]
    companion = COMPAT_COMPANION_TYPES.get(str(event_type or "").strip())
    if companion and companion != primary.type:
        # 兼容伴随帧：**类型名保持旧名**（旧前端按旧类型分派），载荷仍走安全白名单。
        stable = str(
            values.get("entry_id") or values.get("call_id") or values.get("step_id") or ""
        )
        companion_values = dict(values)
        if companion == CanonicalEventType.DONE.value:
            companion_values.setdefault(
                "state", primary.payload.get("state") or "completed"
            )
        out.append(
            EventEnvelope(
                event_id=make_event_id(job_id, companion, stable) if stable else "",
                version=EVENT_ENVELOPE_VERSION,
                schema_version=int(PAYLOAD_SCHEMA_VERSIONS.get(companion, 1)),
                seq=0,
                type=companion,
                trace_id=str(trace_id or ""),
                conversation_id=str(conversation_id or ""),
                job_id=str(job_id or ""),
                occurred_at=str(occurred_at or primary.occurred_at or _now_iso()),
                payload=build_payload(companion, companion_values),
            )
        )
    return out


def dedupe_envelopes(events: list[EventEnvelope]) -> list[EventEnvelope]:
    """按 :attr:`EventEnvelope.dedup_key` 去重（保序；后端与前端同一规则）。"""
    seen: set[str] = set()
    out: list[EventEnvelope] = []
    for event in events or []:
        key = event.dedup_key
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


__all__ = [
    "ALLOWED_PAYLOAD_KEYS",
    "CANONICAL_EVENT_TYPES",
    "COMPAT_COMPANION_TYPES",
    "CanonicalEventType",
    "EVENT_ENVELOPE_VERSION",
    "FORBIDDEN_PAYLOAD_KEYS",
    "LEGACY_EVENT_ALIASES",
    "OPAQUE_PAYLOAD_KEYS",
    "OPAQUE_VALUE_MAX_BYTES",
    "PAYLOAD_MODELS",
    "PAYLOAD_SCHEMA_VERSIONS",
    "SECRET_PAYLOAD_KEYS",
    "TERMINAL_CONTROL_STATES",
    "TEXT_DELTA_RUNAWAY_GUARD",
    "VIEW_EVENT_DATA_MAX_BYTES",
    "VIEW_EVENT_DATA_MAX_DEPTH",
    "VIEW_EVENT_DATA_MAX_ITEMS",    "ApprovalRequiredPayload",
    "ApprovalResolvedPayload",
    "ArtifactCreatedPayload",
    "ControlPayload",
    "ErrorPayload",
    "EventEnvelope",
    "EventPayload",
    "ProcessPayload",
    "StepCompletedPayload",
    "StepStartedPayload",
    "TextDeltaPayload",
    "ViewUpdatedPayload",
    "build_envelope",
    "build_payload",
    "bounded_opaque_value",
    "bound_view_frame",
    "canonical_event_type",
    "canonical_events_for",
    "dedupe_envelopes",
    "is_canonical_event_type",
    "is_terminal_envelope",
    "is_terminal_state",
    "make_event_id",
    "payload_model",
    "scrub_forbidden_keys",
    "strip_unsafe_payload",
    "view_data_within_bounds",
]
