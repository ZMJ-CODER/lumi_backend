"""Lumi enum adapters for the kernel's deterministic replan safety policy."""

from __future__ import annotations

from lumi_orch.replanning import (
    ReplanDecision,
    decide_failed_job_replan as _decide_failed_job_replan,
    decide_logical_plan_replan,
    replan_limit_reached,
)

from app.agents.orchestration.tca import ComplexityLevel
from app.agents.orchestration.validation import ValidationOutcome


def decide_failed_job_replan(
    outcome: ValidationOutcome,
    *,
    current: ComplexityLevel | str | None,
    upgrade_count: int,
    replan_count: int,
    max_replans: int,
    dynamic_enabled: bool = True,
    effectful: bool = False,
) -> ReplanDecision:
    decision = _decide_failed_job_replan(
        may_upgrade=outcome.may_upgrade,
        target=str(outcome.target_level) if outcome.target_level else None,
        category=outcome.category.value,
        current=current.value if isinstance(current, ComplexityLevel) else current,
        upgrade_count=upgrade_count,
        replan_count=replan_count,
        max_replans=max_replans,
        dynamic_enabled=dynamic_enabled,
        effectful=effectful,
    )
    if decision.target is None:
        return decision
    return ReplanDecision(
        allowed=decision.allowed,
        target=ComplexityLevel(decision.target),
        reason=decision.reason,
        blocked_code=decision.blocked_code,
    )


__all__ = [
    "ReplanDecision",
    "decide_failed_job_replan",
    "decide_logical_plan_replan",
    "replan_limit_reached",
]
