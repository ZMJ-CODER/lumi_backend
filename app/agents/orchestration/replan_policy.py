"""Deterministic policy decisions for bounded task replanning.

This module deliberately contains no persistence, planner calls, or node
mutation.  It is the brake in front of the non-deterministic planner: callers
provide the execution facts and receive one explicit decision that can be
audited or persisted by the orchestration service.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.orchestration.tca import ComplexityLevel
from app.agents.orchestration.validation import FailureCategory, ValidationOutcome


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    """Result of applying the bounded replan policy."""

    allowed: bool
    target: ComplexityLevel | None = None
    reason: str = ""
    blocked_code: str | None = None


_M3_REPLAN_CATEGORIES = frozenset(
    {
        FailureCategory.CAPABILITY,
        FailureCategory.PLAN,
        FailureCategory.VALIDATION,
    }
)


def _coerce_level(value: ComplexityLevel | str | None) -> ComplexityLevel:
    """Keep persisted routing values and enum values interchangeable."""
    if isinstance(value, ComplexityLevel):
        return value
    return ComplexityLevel(str(value or ComplexityLevel.M2.value))


def replan_limit_reached(
    *,
    upgrade_count: int,
    replan_count: int,
    max_replans: int,
) -> bool:
    """Return whether either bounded retry counter has been exhausted."""
    limit = max(0, int(max_replans))
    return int(upgrade_count) >= limit or int(replan_count) >= limit


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
    """Decide if a failed job may mount a replacement subgraph.

    The ordering mirrors the existing safety contract: feature flag and
    external effects block first; only then are category-specific target
    levels and retry limits considered.  No LLM or storage call belongs here.
    """
    if not dynamic_enabled:
        return ReplanDecision(False, reason="dynamic replanning disabled", blocked_code="disabled")
    if effectful:
        return ReplanDecision(False, reason="task contains an external effect", blocked_code="effectful_task")
    if not outcome.may_upgrade:
        return ReplanDecision(False, reason="validation outcome is not upgradeable")

    level = _coerce_level(current)
    target = outcome.target_level
    if level == ComplexityLevel.M3 and outcome.category in _M3_REPLAN_CATEGORIES:
        # M3 retries stay at M3 but remain bounded by the replan counter.
        target = ComplexityLevel.M3 if int(replan_count) < int(max_replans) else None
    elif level == ComplexityLevel.M0 and target == ComplexityLevel.M1:
        # A deterministic primitive needs a genuinely different planning
        # method; wrapping it in another rule does not add recovery ability.
        target = ComplexityLevel.M2

    if target is None or replan_limit_reached(
        upgrade_count=upgrade_count,
        replan_count=replan_count,
        max_replans=max_replans,
    ):
        return ReplanDecision(False, target=target, reason="replan limit reached")
    return ReplanDecision(True, target=target)


def decide_logical_plan_replan(
    *,
    dynamic_enabled: bool,
    replan_count: int,
    max_replans: int,
    effectful: bool,
) -> ReplanDecision:
    """Apply the common safety gates for rolling logical-plan replanning."""
    if not dynamic_enabled:
        return ReplanDecision(False, reason="dynamic replanning disabled", blocked_code="disabled")
    if int(replan_count) >= int(max_replans):
        return ReplanDecision(False, reason="replan limit reached", blocked_code="replan_limit")
    if effectful:
        return ReplanDecision(False, reason="task contains an external effect", blocked_code="effectful_task")
    return ReplanDecision(True)
