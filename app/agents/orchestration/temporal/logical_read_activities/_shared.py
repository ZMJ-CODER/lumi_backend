"""_heartbeat _TERMINAL（logical_read_activities 的 _shared 族）。"""

from __future__ import annotations
from temporalio import activity
from app.agents.orchestration.models import JobStatus


def _heartbeat(details: dict) -> None:
    try:
        activity.heartbeat(details)
    except RuntimeError:
        # 单元测试可能在 Activity 上下文之外直接调用实现。
        pass


_TERMINAL = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.INTERRUPTED,
}
