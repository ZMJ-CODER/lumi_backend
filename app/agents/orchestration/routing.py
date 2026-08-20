"""Planning dispatch for TCA execution levels."""

from __future__ import annotations

from app.agents.orchestration.tca import ComplexityLevel


async def plan_for_level(planner, level: ComplexityLevel, *args, **kwargs):
    """Use a level-aware planner when available, preserving custom planners."""
    method = getattr(planner, "plan_for_level", None)
    if callable(method):
        return await method(level, *args, **kwargs)
    return await planner.plan(*args, **kwargs)

