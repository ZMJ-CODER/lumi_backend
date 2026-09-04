"""通过复杂度遥测和统一 LLM 入口选择办公工作流计划。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor


@dataclass(slots=True)
class OfficePlanSelection:
    tree: Any
    routing: dict
    level: ComplexityLevel


class OfficePlanSelectionService:
    """Keep normal-office plan selection separate from Job lifecycle writes."""

    def __init__(
        self,
        *,
        planner: Any,
        workers: dict,
        assessor: TaskComplexityAssessor,
    ) -> None:
        self._planner = planner
        self._workers = workers
        self._assessor = assessor

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
            "replan_count": 0,
            "upgrade_count": 0,
            "upgrades": [],
            "plan_revision": 1,
            "plan_history": [],
        }
        # A workflow plan is a model decision over the current request,
        # attachments, permissions and tool/Skill versions.  Reusing it from a
        # coarse text-pattern cache can silently apply stale dependencies or
        # capabilities.  Keep the cache implementation for explicitly opted-in
        # deterministic workloads, but never use it for office LLM plans.
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
        # LLM-planned dependencies are part of the JobSpec contract.  The
        # executor validates resource conflicts and side effects; it must not
        # silently turn independent nodes into a serial chain merely because
        # they were not produced by an old deterministic shortcut. External
        # legacy Planner implementations retain their old windowing behavior.
        routing["preserve_dependencies"] = any(
            bool((node.metadata or {}).get("planner_generated"))
            or bool((node.metadata or {}).get("preserve_dependencies"))
            for node in (tree.nodes or [])
        )
        duration = time.perf_counter() - started
        routing["route_latency_ms"] = int(duration * 1000)
        try:
            from app.core.observability import inc_agent_route

            inc_agent_route(level.value, assessment.mode.value, False, duration)
        except Exception:  # noqa: BLE001
            pass
        return OfficePlanSelection(
            tree=tree,
            routing=routing,
            level=level,
        )
