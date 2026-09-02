"""有界替代规划使用的确定性安全门。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    allowed: bool
    target: str | None = None
    reason: str = ""
    blocked_code: str | None = None


_M3_REPLAN_CATEGORIES = frozenset({"capability_error", "plan_error", "validation_error"})


def replan_limit_reached(*, upgrade_count: int, replan_count: int, max_replans: int) -> bool:
    limit = max(0, int(max_replans))
    return int(upgrade_count) >= limit or int(replan_count) >= limit


def decide_failed_job_replan(
    *,
    may_upgrade: bool,
    target: str | None,
    category: str,
    current: str | None,
    upgrade_count: int,
    replan_count: int,
    max_replans: int,
    dynamic_enabled: bool = True,
    effectful: bool = False,
) -> ReplanDecision:
    """Decide if a failed job may mount a replacement subgraph.

    Inputs are intentionally primitive values so validation engines, planners
    and runtime adapters can share the same brake without importing each other.
    """
    if not dynamic_enabled:
        return ReplanDecision(False, reason="dynamic replanning disabled", blocked_code="disabled")
    if effectful:
        return ReplanDecision(False, reason="task contains an external effect", blocked_code="effectful_task")
    if not may_upgrade:
        return ReplanDecision(False, reason="validation outcome is not upgradeable")

    current_value = str(current or "m2")
    target_value = str(target) if target else None
    if current_value == "m3" and str(category) in _M3_REPLAN_CATEGORIES:
        target_value = "m3" if int(replan_count) < int(max_replans) else None
    elif current_value == "m0" and target_value == "m1":
        target_value = "m2"

    if target_value is None or replan_limit_reached(
        upgrade_count=upgrade_count,
        replan_count=replan_count,
        max_replans=max_replans,
    ):
        return ReplanDecision(False, target=target_value, reason="replan limit reached")
    return ReplanDecision(True, target=target_value)


def decide_logical_plan_replan(
    *,
    dynamic_enabled: bool,
    replan_count: int,
    max_replans: int,
    effectful: bool,
) -> ReplanDecision:
    if not dynamic_enabled:
        return ReplanDecision(False, reason="dynamic replanning disabled", blocked_code="disabled")
    if int(replan_count) >= int(max_replans):
        return ReplanDecision(False, reason="replan limit reached", blocked_code="replan_limit")
    if effectful:
        return ReplanDecision(False, reason="task contains an external effect", blocked_code="effectful_task")
    return ReplanDecision(True)
