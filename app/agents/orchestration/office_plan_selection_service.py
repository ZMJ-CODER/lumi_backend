"""Select a normal office plan through assessment, cache, and planner routing."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from app.agents.orchestration.plan_cache import PlanCache, build_plan_cache_key
from app.agents.orchestration.plan_context import PlanRequestContext
from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor


@dataclass(slots=True)
class OfficePlanSelection:
    tree: Any
    routing: dict
    level: ComplexityLevel
    cache_key: str
    cache_hit: bool


class OfficePlanSelectionService:
    """Keep normal-office plan selection separate from Job lifecycle writes."""

    def __init__(
        self,
        *,
        planner: Any,
        workers: dict,
        assessor: TaskComplexityAssessor,
        plan_cache: PlanCache,
    ) -> None:
        self._planner = planner
        self._workers = workers
        self._assessor = assessor
        self._plan_cache = plan_cache

    async def select(
        self,
        *,
        user_id: str,
        request: str,
        user_role: str,
        project_id: str | None,
        project_ids: list[str] | None,
        clarification_answer: str | None,
        office_docs: list[dict] | None,
        prior_summaries: str,
        planning_context: PlanRequestContext,
        routing_model: dict,
    ) -> OfficePlanSelection:
        started = time.perf_counter()
        assessment = await self._assessor.assess(
            request,
            office_docs=office_docs,
            prior_summaries=prior_summaries,
        )
        level = assessment.level
        routing = {
            "llm": routing_model,
            **assessment.audit_dict(),
            "cache_hit": False,
            "replan_count": 0,
            "upgrade_count": 0,
            "upgrades": [],
            "plan_revision": 1,
            "plan_history": [],
        }
        capability_parts = [f"worker:{name}" for name in sorted(self._workers)]
        try:
            from app.agents.skills.registry import SkillRegistry

            capability_parts.extend(
                f"skill:{skill.name}:{skill.category}:{skill.environment}:"
                f"{int(skill.write_op)}:{int(skill.idempotent)}"
                for skill in sorted(SkillRegistry.list(), key=lambda item: item.name)
            )
        except Exception:  # noqa: BLE001
            pass
        capability_signature = hashlib.sha256(
            "|".join(capability_parts).encode("utf-8")
        ).hexdigest()[:16]
        cache_allowed = (
            level in {ComplexityLevel.M1, ComplexityLevel.M2}
            and not project_id
            and not project_ids
            and not clarification_answer
            and not prior_summaries
        )
        cache_key = ""
        cache_hit = False
        tree = None
        if cache_allowed:
            cache_key = build_plan_cache_key(
                user_id=user_id,
                request=request,
                scene="office",
                user_role=user_role,
                office_docs=office_docs,
                capability_signature=capability_signature,
            )
            cached = await self._plan_cache.get(cache_key, office_docs)
            try:
                from app.core.observability import inc_plan_cache

                inc_plan_cache("hit" if cached else "miss")
            except Exception:  # noqa: BLE001
                pass
            if cached:
                from app.agents.orchestration.planner import TaskTree

                cached_nodes, cached_plan_text = cached
                tree = TaskTree(nodes=cached_nodes, plan_text=cached_plan_text)
                cache_hit = True
        if tree is None:
            from app.agents.orchestration.routing import plan_for_level

            tree = await plan_for_level(
                self._planner,
                level,
                user_id,
                request,
                "office",
                project_id,
                project_ids,
                planning_context.llm_api_key,
                clarification_answer,
                office_docs,
                prior_summaries,
                context=planning_context,
            )
        routing["cache_hit"] = cache_hit
        duration = time.perf_counter() - started
        routing["route_latency_ms"] = int(duration * 1000)
        try:
            from app.core.observability import inc_agent_route

            inc_agent_route(level.value, assessment.mode.value, cache_hit, duration)
        except Exception:  # noqa: BLE001
            pass
        return OfficePlanSelection(
            tree=tree,
            routing=routing,
            level=level,
            cache_key=cache_key,
            cache_hit=cache_hit,
        )
