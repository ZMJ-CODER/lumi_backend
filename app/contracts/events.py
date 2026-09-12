"""SSE 事件契约桥接（第四阶段）：内部事件字典 → 版本化流式帧。

背景：``/chat/stream``、``/agents/jobs/{id}/resume?action=run_next`` 等入口各自
`json.dumps` 裸事件字典，前端无法判断"事件属于哪个契约版本"，也无法发现丢帧。

因此这里集中一件事：把内部事件字典编码为 ``StreamEvent`` 帧，附带

* ``version``：事件契约版本（前端可按版本解析，未知事件直接忽略）；
* ``seq``：**每条流内单调递增**的序号（前端可据此发现缺口）。

编码必须是**无损**的：原有扁平字段一个不改、一个不少，只是多了 ``version`` /
``seq``；未知事件类型也不得抛错（后端不因为新类型失败）。

**过渡期双投影（方案第一阶段）**：内部只生成一套标准事件
（``app.contracts.event_adapter`` + ``lumi_contracts.events.envelope``），本编码器
负责把它投影成两种 SSE 形状：

* ``legacy``（默认，前端当前消费的形状）：扁平字段 + ``version`` / ``seq``，
  与历史输出逐字节一致；
* ``canonical``（开关 ``STREAM_EVENT_PROTOCOL=canonical``）：统一信封
  ``event_id/version/seq/type/trace_id/conversation_id/job_id/occurred_at/payload``。

业务代码不需要知道投影方式：两种投影来自同一份标准事件。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lumi_contracts import EventSequencer, ProcessLogEntry, StreamEvent
from lumi_contracts.events.envelope import bound_view_frame, scrub_forbidden_keys

# 事件契约版本：新增字段不升版本，破坏性改名才升（前端按版本解析）。
STREAM_EVENT_VERSION = 1

# 需要补"过程条目字段"的事件：过程气泡只消费这些，普通聊天 delta/done 不受影响。
# "step" / "plan_ready" 是自动（auto/step_confirm）路径的实时帧，之前漏登记，
# 导致那条路径实时没有统一字段（只能靠刷新恢复）。
_PROCESS_EVENT_TYPES = frozenset({
    "process",
    "step",
    "step_started",
    "step_completed",
    "plan_ready",
    "tool",
    "tool_started",
    "tool_completed",
    "approval_required",
    "approval_resolved",
    # 能力状态帧（阶段 2）：与过程帧共用统一字段，前端在同一气泡里渲染
    # "等待 Provider / 能力完成 / 本地拒止"。delta/done 帧完全不受影响。
    "capability_requested",
    "waiting_provider",
    "provider_connected",
    "provider_disconnected",
    "capability_started",
    "capability_completed",
    "capability_failed",
    "plugin_health_changed",
    # 工作区操作事件（统一 OperationResult）：与能力帧同一气泡里渲染
    # "开始/预览/等待确认/完成/失败/已回滚"。
    "operation_started",
    "operation_preview",
    "operation_completed",
    "operation_failed",
    "operation_rolled_back",
})


#: 能力状态帧的类型集合（它们的 ``status`` 用能力词表，不是过程状态词表）。
_CAPABILITY_STATUS_EVENT_TYPES = frozenset(
    {
        "capability_requested",
        "waiting_provider",
        "provider_connected",
        "provider_disconnected",
        "capability_started",
        "capability_completed",
        "capability_failed",
        "plugin_health_changed",
    }
)

#: 操作帧的类型集合（``status`` 用操作状态词表：no_change / already_absent / denied / …）。
_OPERATION_STATUS_EVENT_TYPES = frozenset(
    {
        "operation_started",
        "operation_preview",
        "operation_completed",
        "operation_failed",
        "operation_rolled_back",
    }
)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


#: 能力状态帧的展示文案模板（kind 由后端判定；前端只渲染，不按能力名猜）。
_CAPABILITY_EVENT_TITLE = {
    "capability_requested": "请求能力",
    "waiting_provider": "等待客户端 Provider",
    "provider_connected": "Provider 已连接",
    "provider_disconnected": "Provider 已断开",
    "capability_started": "正在执行能力",
    "capability_completed": "能力已完成",
    "capability_failed": "能力未完成",
    "plugin_health_changed": "插件健康状态变化",
    "approval_required": "等待确认",
    # 工作区操作帧（operation 名由后端给，前端不猜）。
    "operation_started": "开始工作区操作",
    "operation_preview": "操作预览",
    "operation_completed": "工作区操作完成",
    "operation_failed": "工作区操作未完成",
    "operation_rolled_back": "工作区操作已回滚",
}

_CAPABILITY_EVENT_SUMMARY = {
    "capability_requested": "正在准备调用 {capability}",
    "waiting_provider": "{capability} 暂无可用 Provider，等待客户端连接",
    "provider_connected": "{provider} 已提供 {capability}",
    "provider_disconnected": "{provider} 已断开，{capability} 暂时不可用",
    "capability_started": "正在由 {provider} 执行 {capability}",
    "capability_completed": "{capability} 已完成",
    "capability_failed": "{capability} 未完成：{error_code}",
    "plugin_health_changed": "插件健康状态：{health_status}",
    "approval_required": "{capability} 需要你确认后继续",
    # 工作区操作帧：只报路径与状态，正文永远不进事件流。
    "operation_started": "正在执行 {operation}：{path}",
    "operation_preview": "{operation} 预览：{path}（尚未执行）",
    "operation_completed": "{operation} 完成：{path}",
    "operation_failed": "{operation} 未完成：{path}（{error_code}）",
    "operation_rolled_back": "{operation} 已回滚：{path}",
}


def _capability_display_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    """能力/操作状态帧的 title/summary（缺省时按事件类型现算，避免空行）。

    与过程帧同样的道理：出口只能复制它拿到的字段，所以这里给兜底文案；
    文案里只放能力名/Provider/错误码/健康状态/工作区相对路径，不涉及参数与正文。
    """
    event_type = str(payload.get("type") or "")
    if event_type not in _CAPABILITY_EVENT_TITLE:
        return {}
    capability = str(payload.get("capability") or "该能力")
    provider = str(payload.get("provider_id") or "客户端")
    operation = str(payload.get("operation") or "操作")
    path = str(payload.get("logical_path") or payload.get("target_path") or "（未指定路径）")
    fields: dict[str, str] = {
        "title": _CAPABILITY_EVENT_TITLE[event_type],
        "summary": _CAPABILITY_EVENT_SUMMARY[event_type].format(
            capability=capability,
            provider=provider,
            operation=operation,
            path=path,
            error_code=str(payload.get("error_code") or "未说明原因"),
            health_status=str(payload.get("health_status") or "unknown"),
        ),
    }
    if capability and capability != "该能力":
        fields["tool_name"] = capability[:80]
    elif event_type in _OPERATION_STATUS_EVENT_TYPES:
        fields["tool_name"] = f"workspace_{operation}"[:80]
    return fields


def encode_sse(frame: Mapping[str, Any]) -> str:
    """把已经带 ``version`` / ``seq`` 的帧编码成 SSE 行。"""
    # default=str 兜底：citations 等元数据里若混入 datetime 等类型，
    # 序列化失败会让整条流式以 error 结束，绝不能发生。
    return f"data: {json.dumps(dict(frame), ensure_ascii=False, default=str)}\n\n"


class SseEventEncoder:
    """一条流的 SSE 编码器：保证 ``seq`` 单调递增，且载荷无损。

    ``protocol`` 决定投影形状（``legacy`` 默认 / ``canonical``）；两种投影都来自
    同一份标准事件，调用方不需要维护第二套事件逻辑。
    """

    def __init__(
        self,
        *,
        version: int = STREAM_EVENT_VERSION,
        job_id: str = "",
        conversation_id: str = "",
        trace_id: str = "",
        start_seq: int = 0,
        protocol: str = "",
    ) -> None:
        self._sequencer = EventSequencer(start=start_seq)
        self._version = int(version)
        self._job_id = str(job_id or "")
        self._conversation_id = str(conversation_id or "")
        self._trace_id = str(trace_id or "")
        self._protocol = str(protocol or "").strip().casefold()
        self._last_seq = int(start_seq)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def protocol(self) -> str:
        """当前投影协议：``legacy``（默认）或 ``canonical``。"""
        if self._protocol in {"legacy", "canonical"}:
            return self._protocol
        try:
            from app.core.config import settings

            configured = str(getattr(settings, "STREAM_EVENT_PROTOCOL", "legacy") or "legacy")
        except Exception:  # noqa: BLE001 - 配置不可用时保持旧协议（前端兼容优先）
            configured = "legacy"
        return "canonical" if configured.strip().casefold() == "canonical" else "legacy"

    def frame(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """事件字典 → 扁平 SSE 帧（``type`` / ``version`` / ``seq`` + 原字段）。

        过程/工具事件额外补齐**统一展示字段**（``entry_id`` / ``kind`` / ``title`` /
        ``summary`` / ``detail`` / ``status`` / ``step_id`` / ``call_id`` /
        ``sequence`` / ``occurred_at``）：前端只渲染，不再按工具名猜"读取/编辑/Pwsh"，
        也不需要第二套过程解析入口。原始参数/响应/推理文本一律不取。

        **无损 ≠ 无脱敏**：扁平字段一个不少，但方案 §1.2 的禁入字段（原始思维链、
        工具参数、原始结果、凭据、堆栈）在出口处被结构性删除——旧投影与标准投影
        在同一道安全边界内。
        """
        payload = dict(event or {})
        seq = self._sequencer.next_seq()
        self._last_seq = seq
        event_type = str(payload.get("type") or "error")
        if event_type in {"view_updated", "view"}:
            # 视图通道级上限（方案 §2.3）：旧投影同样不能把超限 data 塞进事件流。
            payload = bound_view_frame(payload)
        if event_type in _PROCESS_EVENT_TYPES:
            # 能力状态帧先补兜底展示文案（能力名/Provider/错误码），再交给统一条目，
            # 避免发射方漏给 title/summary 时前端出现空行。
            payload.update(_capability_display_fields(payload))
            entry = ProcessLogEntry.from_event(
                payload,
                job_id=str(payload.get("job_id") or self._job_id or ""),
                sequence=seq,
                occurred_at=str(payload.get("occurred_at") or _now_iso()),
            )
            if not entry.sequence:
                entry = entry.model_copy(update={"sequence": seq})
            # 原字段保留（旧前端兼容），统一字段覆盖同名键。
            payload.update(entry.to_sse_fields())
            if event_type in _CAPABILITY_STATUS_EVENT_TYPES and payload.get("status"):
                # 能力帧的 ``status`` 是**能力状态词表**（waiting_provider/unavailable/
                # denied/…），契约过程状态只有 pending/running/completed/failed。
                # 两者不能互相覆盖：这里保留能力状态给前端分派，同时把过程状态放在
                # ``process_status`` 里，让按过程契约消费的旧读法仍然拿得到合法值。
                payload["process_status"] = str(entry.status)
                payload["status"] = str(event.get("status") or entry.status)
            elif event_type in _OPERATION_STATUS_EVENT_TYPES and payload.get("status"):
                # 操作帧同理：no_change / already_absent / denied 都不是过程状态值，
                # 必须原样保留给前端，过程状态放 process_status。
                payload["process_status"] = str(entry.status)
                payload["status"] = str(event.get("status") or entry.status)
        return StreamEvent(
            type=event_type,
            version=self._version,
            seq=seq,
            job_id=str(payload.get("job_id") or self._job_id or ""),
            conversation_id=str(payload.get("conversation_id") or self._conversation_id or ""),
            call_id=str(payload.get("call_id") or ""),
            step_id=str(payload.get("step_id") or ""),
            data=scrub_forbidden_keys(payload),
        ).to_sse()

    def canonical_frames(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """事件字典 → 标准信封帧（1..2 帧；终态事件附兼容伴随帧）。

        载荷来自 :mod:`app.contracts.event_adapter`：原始思维链/工具参数/完整结果
        在适配器与契约工厂两处被白名单剔除，因此新投影天然不含这些字段。
        """
        from app.contracts.event_adapter import canonical_events

        envelopes = canonical_events(
            event,
            job_id=self._job_id,
            conversation_id=self._conversation_id,
            trace_id=self._trace_id,
        )
        frames: list[dict[str, Any]] = []
        for envelope in envelopes:
            seq = self._sequencer.next_seq()
            self._last_seq = seq
            frame = envelope.with_seq(seq).to_canonical_frame()
            frame["payload"] = self._bind_process_sequence(frame.get("payload"), seq)
            frames.append(frame)
        return frames

    def _bind_process_sequence(self, payload: Any, seq: int) -> dict[str, Any]:
        """把过程条目的 ``sequence``/``entry_id`` 绑到本流序号（与旧投影同规则）。

        适配器不知道流序号，因此过程帧先按"仅 job 已知"构造；这里补齐后，
        SSE 重连/快照恢复的同一条日志仍然命中同一个 ``entry_id``。
        """
        data = dict(payload) if isinstance(payload, Mapping) else {}
        if not data or "kind" not in data:
            return data
        try:
            current = int(data.get("sequence") or 0)
        except (TypeError, ValueError):
            current = 0
        if current:
            return data
        data["sequence"] = int(seq)
        entry_id = str(data.get("entry_id") or "")
        if not entry_id or entry_id.startswith("seq:"):
            data["entry_id"] = f"seq:{self._job_id}:{int(seq)}"
        return data

    def frames(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """按当前协议产出帧列表（``legacy`` → 1 帧；``canonical`` → 1..2 帧）。"""
        if self.protocol == "canonical":
            return self.canonical_frames(event)
        return [self.frame(event)]

    def encode(self, event: Mapping[str, Any]) -> str:
        """事件字典 → SSE 行（含版本与序号）。"""
        return encode_sse(self.frame(event))

    def encode_all(self, event: Mapping[str, Any]) -> list[str]:
        """按当前协议产出 SSE 行列表（切换新协议时调用方只需改这一个方法）。"""
        return [encode_sse(frame) for frame in self.frames(event)]

    def encode_frames(self, event: Mapping[str, Any]) -> list[tuple[dict[str, Any], str]]:
        """按当前协议产出 ``(帧, SSE 行)`` 列表。

        需要把帧同时写进任务事件日志（断线续传）的出口用这个：直接拿到帧对象，
        不必再解析已经序列化好的 SSE 行。
        """
        return [(frame, encode_sse(frame)) for frame in self.frames(event)]


__all__ = ["STREAM_EVENT_VERSION", "SseEventEncoder", "encode_sse"]
