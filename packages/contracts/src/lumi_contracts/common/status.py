"""统一执行状态。

与内部实现解耦：``ToolOutput.status``（历史字符串枚举）、DAG 节点状态、SSE 状态
都需要能投影到同一组"执行状态"上，避免每个模块各自一套字符串比较。
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionStatus(StrEnum):
    """一次工具/Skill/节点执行的统一状态。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    EMPTY = "empty"
    PENDING = "pending"
    PENDING_APPROVAL = "pending_approval"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_ok(self) -> bool:
        """是否属于"有可用结果"的终态（partial/empty 也算可用）。"""
        return self in {ExecutionStatus.SUCCESS, ExecutionStatus.PARTIAL, ExecutionStatus.EMPTY}

    @property
    def is_terminal(self) -> bool:
        return self not in {ExecutionStatus.PENDING, ExecutionStatus.PENDING_APPROVAL}

    @classmethod
    def coerce(cls, value: object, *, default: "ExecutionStatus | None" = None) -> "ExecutionStatus":
        """宽松归一：未知值折叠为 default，不抛异常（跨边界兼容旧插件）。"""
        fallback = default or ExecutionStatus.FAILED
        text = str(value or "").strip().casefold()
        if not text:
            return fallback
        for item in cls:
            if item.value == text:
                return item
        # 历史别名：ok/complete/done 都表示成功
        if text in {"ok", "complete", "completed", "done"}:
            return cls.SUCCESS
        if text in {"error", "exception"}:
            return cls.FAILED
        return fallback


__all__ = ["ExecutionStatus"]
