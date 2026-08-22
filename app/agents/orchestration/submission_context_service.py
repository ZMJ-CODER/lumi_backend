"""Prepare the immutable context shared by planning and execution.

Submission code needs one trusted attachment view, one bounded office-memory
view, and one frozen effective model configuration.  Centralizing this prevents
different submission branches from resolving a different model or trusting
different client attachment metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.orchestration.memory_service import OfficeMemoryService
from app.agents.orchestration.plan_context import PlanRequestContext
from app.core.llm_config import EffectiveLLMConfig, resolve_effective_llm_config


@dataclass(slots=True)
class SubmissionContext:
    office_docs: list[dict]
    prior_summaries: str
    presentation_preferences: str
    effective_llm: EffectiveLLMConfig
    planning_context: PlanRequestContext


class SubmissionContextService:
    """Resolve trusted office inputs and a job-scoped LLM snapshot."""

    def __init__(self, *, memory: OfficeMemoryService) -> None:
        self._memory = memory

    async def prepare(
        self,
        *,
        user_id: str,
        request: str,
        scene: str,
        conversation_id: str | None,
        project_id: str | None,
        project_ids: list[str] | None,
        request_api_key: str | None,
        clarification_answer: str | None,
        office_docs: list[dict] | None,
    ) -> SubmissionContext:
        verified_docs: list[dict] = []
        prior_summaries = ""
        presentation_preferences = ""
        if scene == "office":
            verified_docs = await self._memory.verify_documents(
                user_id, request, office_docs
            )
            prior_summaries = await self._memory.load_recall_context(
                user_id, request, conversation_id
            )
            if not prior_summaries and conversation_id:
                try:
                    from app.services.office_task_memory import needs_office_task_recall

                    if needs_office_task_recall(request):
                        prior_summaries = await self._memory.load_summaries(conversation_id)
                except Exception:  # noqa: BLE001
                    pass
            presentation_preferences = await self._memory.load_presentation_preferences(
                user_id
            )
        effective_llm = await resolve_effective_llm_config(
            scene=scene,
            user_id=user_id,
            request_api_key=request_api_key,
        )
        llm_config = effective_llm.as_dict()
        planning_context = PlanRequestContext.from_legacy_args(
            user_id=user_id,
            request=request,
            scene=scene,
            project_id=project_id,
            project_ids=project_ids,
            llm_api_key=effective_llm.api_key,
            llm_config=llm_config,
            clarification_answer=clarification_answer,
            office_docs=verified_docs,
            prior_summaries=prior_summaries,
        )
        return SubmissionContext(
            office_docs=verified_docs,
            prior_summaries=prior_summaries,
            presentation_preferences=presentation_preferences,
            effective_llm=effective_llm,
            planning_context=planning_context,
        )
