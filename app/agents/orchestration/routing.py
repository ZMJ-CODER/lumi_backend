"""按 TCA 执行等级分派规划。"""

from __future__ import annotations

from app.agents.orchestration.tca import ComplexityLevel
from app.agents.orchestration.planning.context import PlanRequestContext


async def plan_for_level(
    planner,
    level: ComplexityLevel,
    *args,
    context: PlanRequestContext | None = None,
    **kwargs,
):
    """Use a level-aware planner when available, preserving custom planners."""
    method = getattr(planner, "plan_for_level", None)
    if callable(method):
        if context is not None and getattr(planner, "supports_context_planning", False):
            return await method(level, context=context, **kwargs)
        return await method(level, *args, **kwargs)
    if context is not None:
        return await planner.plan_context(context)
    return await planner.plan(*args, **kwargs)
