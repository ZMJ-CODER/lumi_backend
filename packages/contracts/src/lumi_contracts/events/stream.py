"""统一流式事件契约。

事件集合（与现有 SSE 对齐）：``task_router`` / ``process`` / ``delta`` /
``tool_started`` / ``tool_completed`` / ``waiting_next`` / ``done`` / ``task_failed``。

三条硬约束：

* 每个事件带 ``type`` / ``version`` / 单调递增 ``seq``，前端可按版本解析；
* **未知事件类型不得导致前端白屏**（前端应忽略未知 type）；后端也不因未知字段失败；
* 事件载荷是投影后的数据（模型文本、UI 字段、审计摘要），**不塞原始 payload**。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StreamEventType(StrEnum):
    JOB = "job"
    TASK_ROUTER = "task_router"
    PROCESS = "process"
    DELTA = "delta"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    STEP = "step"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    PLAN_DELTA = "plan_delta"
    PLAN_READY = "plan_ready"
    WAITING_NEXT = "waiting_next"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    DONE = "done"
    ERROR = "error"


# 前端必须容忍未知事件：这里登记的是当前已知集合，新增不需要前端同步发布。
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(item.value for item in StreamEventType)


class StreamEvent(BaseModel):
    """统一流式事件（版本化 + 递增序列号）。"""

    model_config = ConfigDict(extra="allow")

    type: str
    version: int = 1
    seq: int = 0
    job_id: str = ""
    conversation_id: str = ""
    # 关联到具体调用/步骤（可选）。
    call_id: str = ""
    step_id: str = ""
    # 各事件类型的载荷：delta→content，tool_*→tool/status，done→status/content …
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_known(self) -> bool:
        return str(self.type) in KNOWN_EVENT_TYPES

    def to_sse(self) -> dict[str, Any]:
        """渲染为 SSE 帧：保留扁平结构（前端按 ``type`` 分派），并附版本与序号。"""
        frame: dict[str, Any] = {
            "type": str(self.type),
            "version": int(self.version),
            "seq": int(self.seq),
        }
        if self.job_id:
            frame["job_id"] = self.job_id
        if self.conversation_id:
            frame["conversation_id"] = self.conversation_id
        if self.call_id:
            frame["call_id"] = self.call_id
        if self.step_id:
            frame["step_id"] = self.step_id
        for key, value in self.data.items():
            frame.setdefault(str(key), value)
        return frame


class EventSequencer:
    """为一条流分配单调递增的 ``seq``（同一 job 内共享）。"""

    def __init__(self, *, start: int = 0) -> None:
        self._next = int(start)

    def next_seq(self) -> int:
        self._next += 1
        return self._next

    def wrap(self, event_type: str | StreamEventType, **payload: Any) -> StreamEvent:
        return StreamEvent(type=str(event_type), seq=self.next_seq(), data=dict(payload))


__all__ = [
    "EventSequencer",
    "KNOWN_EVENT_TYPES",
    "StreamEvent",
    "StreamEventType",
]
