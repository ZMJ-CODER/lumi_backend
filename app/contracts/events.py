"""SSE 事件契约桥接（第四阶段）：内部事件字典 → 版本化流式帧。

背景：``/chat/stream``、``/agents/jobs/{id}/resume?action=run_next`` 等入口各自
`json.dumps` 裸事件字典，前端无法判断"事件属于哪个契约版本"，也无法发现丢帧。

因此这里集中一件事：把内部事件字典编码为 ``StreamEvent`` 帧，附带

* ``version``：事件契约版本（前端可按版本解析，未知事件直接忽略）；
* ``seq``：**每条流内单调递增**的序号（前端可据此发现缺口）。

编码必须是**无损**的：原有扁平字段一个不改、一个不少，只是多了 ``version`` /
``seq``；未知事件类型也不得抛错（后端不因为新类型失败）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lumi_contracts import EventSequencer, ProcessLogEntry, StreamEvent

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
})


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def encode_sse(frame: Mapping[str, Any]) -> str:
    """把已经带 ``version`` / ``seq`` 的帧编码成 SSE 行。"""
    # default=str 兜底：citations 等元数据里若混入 datetime 等类型，
    # 序列化失败会让整条流式以 error 结束，绝不能发生。
    return f"data: {json.dumps(dict(frame), ensure_ascii=False, default=str)}\n\n"


class SseEventEncoder:
    """一条流的 SSE 编码器：保证 ``seq`` 单调递增，且载荷无损。"""

    def __init__(
        self,
        *,
        version: int = STREAM_EVENT_VERSION,
        job_id: str = "",
        conversation_id: str = "",
        start_seq: int = 0,
    ) -> None:
        self._sequencer = EventSequencer(start=start_seq)
        self._version = int(version)
        self._job_id = str(job_id or "")
        self._conversation_id = str(conversation_id or "")
        self._last_seq = int(start_seq)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def frame(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """事件字典 → 扁平 SSE 帧（``type`` / ``version`` / ``seq`` + 原字段）。

        过程/工具事件额外补齐**统一展示字段**（``entry_id`` / ``kind`` / ``title`` /
        ``summary`` / ``detail`` / ``status`` / ``step_id`` / ``call_id`` /
        ``sequence`` / ``occurred_at``）：前端只渲染，不再按工具名猜"读取/编辑/Pwsh"，
        也不需要第二套过程解析入口。原始参数/响应/推理文本一律不取。
        """
        payload = dict(event or {})
        seq = self._sequencer.next_seq()
        self._last_seq = seq
        event_type = str(payload.get("type") or "error")
        if event_type in _PROCESS_EVENT_TYPES:
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
        return StreamEvent(
            type=event_type,
            version=self._version,
            seq=seq,
            job_id=str(payload.get("job_id") or self._job_id or ""),
            conversation_id=str(payload.get("conversation_id") or self._conversation_id or ""),
            call_id=str(payload.get("call_id") or ""),
            step_id=str(payload.get("step_id") or ""),
            data=payload,
        ).to_sse()

    def encode(self, event: Mapping[str, Any]) -> str:
        """事件字典 → SSE 行（含版本与序号）。"""
        return encode_sse(self.frame(event))


__all__ = ["STREAM_EVENT_VERSION", "SseEventEncoder", "encode_sse"]
