"""动态升级决策树与上下文适配策略（纯函数）。

约束：
  - ``CONTEXT_TOO_LARGE`` 不再触发复杂度升级，由 Resolver 内部 smart_slice 处理；
  - 只有 ``DEPENDENCY_REQUIRED`` / ``PATH_UNKNOWN`` / ``MULTI_STEP_REQUIRED``
    允许升级，且映射固定；
  - 超长且需要跨段综合分析时，返回 ``MULTI_STEP_REQUIRED``（建议 M2），
    不抛错、不阻断当前轮。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lumi_orch.task_assessment import Complexity, max_complexity


class UpgradeReason(str, Enum):
    SUCCESS = "SUCCESS"
    DEPENDENCY_REQUIRED = "DEPENDENCY_REQUIRED"
    PATH_UNKNOWN = "PATH_UNKNOWN"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    MULTI_STEP_REQUIRED = "MULTI_STEP_REQUIRED"
    FAILED = "FAILED"


class ContextFitStatus(str, Enum):
    OK = "OK"
    SLICED = "SLICED"
    MULTI_STEP_REQUIRED = "MULTI_STEP_REQUIRED"


@dataclass(frozen=True, slots=True)
class ContextPlan:
    status: ContextFitStatus
    keep_chars: int
    reason: str = ""


_UPGRADE_MAP: dict[UpgradeReason, Complexity | None] = {
    UpgradeReason.DEPENDENCY_REQUIRED: "M2",
    UpgradeReason.PATH_UNKNOWN: "M3",
    UpgradeReason.CONTEXT_TOO_LARGE: None,   # 不升级：Resolver 内部处理
    UpgradeReason.MULTI_STEP_REQUIRED: "M2",
    UpgradeReason.SUCCESS: None,
    UpgradeReason.FAILED: None,
}


def next_complexity(reason: UpgradeReason | str, current: Complexity) -> Complexity | None:
    """按决策树给出升级后的复杂度；None 表示不升级。"""
    key = reason if isinstance(reason, UpgradeReason) else UpgradeReason(str(reason))
    target = _UPGRADE_MAP.get(key)
    if target is None:
        return None
    return max_complexity(current, target)


def plan_context_fit(
    *,
    text_length: int,
    size_limit: int,
    requires_cross_segment: bool = False,
) -> ContextPlan:
    """内容适配策略：绝不返回 CONTEXT_TOO_LARGE。

    - 未超限 → OK；
    - 超限且单次读取任务 → SLICED（smart_slice 只返回相关片段）；
    - 超限且需要跨段综合分析 → MULTI_STEP_REQUIRED（建议升级 M2）。
    """
    limit = max(1, int(size_limit))
    length = max(0, int(text_length))
    if length <= limit:
        return ContextPlan(ContextFitStatus.OK, keep_chars=length)
    if requires_cross_segment:
        return ContextPlan(
            ContextFitStatus.MULTI_STEP_REQUIRED,
            keep_chars=limit,
            reason="内容超出单次窗口且需要跨段综合分析",
        )
    return ContextPlan(ContextFitStatus.SLICED, keep_chars=limit, reason="已按相关性截取片段")


__all__ = [
    "ContextFitStatus",
    "ContextPlan",
    "UpgradeReason",
    "next_complexity",
    "plan_context_fit",
]
