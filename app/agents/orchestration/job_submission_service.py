"""Office-job planning, materialization, and runtime submission.

Admission locking stays at the orchestrator boundary.  Once that lock is held,
this service owns the linear submission transaction: prepare context, choose a
plan, materialize a job, promote capacity, and select the runtime backend.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.orchestration.job_materialization_service import JobMaterializationService
from app.agents.orchestration.manifest_submission_service import ManifestSubmissionService
from app.agents.orchestration.models import Job
from app.agents.orchestration.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.plan_compilation_service import PlanCompilationService
from app.agents.orchestration.plan_context import PlanRequestContext
from app.agents.orchestration.submission_context_service import SubmissionContextService
from app.agents.orchestration.admission import job_admission
from app.repositories.job_repository import JobRepository


class JobSubmissionService:
    """Execute an already-admitted submission without depending on the facade."""

    def __init__(
        self,
        *,
        store: JobRepository,
        context_service: SubmissionContextService,
        manifest_submission: ManifestSubmissionService,
        office_plan_selection: OfficePlanSelectionService,
        plan_compilation: PlanCompilationService,
        materialization: JobMaterializationService,
        temporal_mode: bool,
        can_run_manifest_temporal: Callable[[Job], bool],
        probe_temporal: Callable[[], Awaitable[bool]],
        manifest_backend: Any,
        legacy_backend: Any,
        start_heartbeat: Callable[[str, str], None],
        stop_heartbeat: Callable[[str], Awaitable[None]],
        plan_with_context: Callable[[PlanRequestContext], Awaitable[Any]],
        plan_contexts: dict[str, dict],
        llm_configs: dict[str, dict],
        pending_plan_cache: dict[str, tuple[str, list[dict] | None]],
    ) -> None:
        self._store = store
        self._context_service = context_service
        self._manifest_submission = manifest_submission
        self._office_plan_selection = office_plan_selection
        self._plan_compilation = plan_compilation
        self._materialization = materialization
        self._temporal_mode = temporal_mode
        self._can_run_manifest_temporal = can_run_manifest_temporal
        self._probe_temporal = probe_temporal
        self._manifest_backend = manifest_backend
        self._legacy_backend = legacy_backend
        self._start_heartbeat = start_heartbeat
        self._stop_heartbeat = stop_heartbeat
        self._plan_with_context = plan_with_context
        self._plan_contexts = plan_contexts
        self._llm_configs = llm_configs
        self._pending_plan_cache = pending_plan_cache

    def _discard_pending(self, job_id: str) -> None:
        self._plan_contexts.pop(job_id, None)
        self._pending_plan_cache.pop(job_id, None)

    async def submit(
        self,
        *,
        user_id: str,
        request: str,
        scene: str,
        conversation_id: str | None,
        project_id: str | None,
        project_ids: list[str] | None,
        llm_api_key: str | None,
        clarification_answer: str | None,
        office_docs: list[dict] | None,
        user_role: str,
        submission_key: str,
        admission_token: str,
    ) -> Job:
        """Create and dispatch one job while an admission reservation is held."""
        prepared = await self._context_service.prepare(
            user_id=user_id,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            project_id=project_id,
            project_ids=project_ids,
            request_api_key=llm_api_key,
            clarification_answer=clarification_answer,
            office_docs=office_docs,
        )
        office_docs = prepared.office_docs
        effective_llm = prepared.effective_llm
        llm_config = effective_llm.as_dict()
        routing_model = effective_llm.public_dict()
        planning_context = prepared.planning_context
        routing: dict = {"llm": routing_model} if scene == "office" else {}
        cache_key = ""
        cache_hit = False

        manifest_submission = (
            await self._manifest_submission.prepare(
                user_id=user_id,
                request=request,
                office_docs=office_docs,
                llm_api_key=effective_llm.api_key,
                llm_config=llm_config,
                routing_model=routing_model,
            )
            if scene == "office"
            else None
        )
        if manifest_submission is not None:
            tree = manifest_submission.tree
            routing = manifest_submission.routing
        elif scene == "office":
            selection = await self._office_plan_selection.select(
                user_id=user_id,
                request=request,
                user_role=user_role,
                project_id=project_id,
                project_ids=project_ids,
                clarification_answer=clarification_answer,
                office_docs=office_docs,
                prior_summaries=prepared.prior_summaries,
                planning_context=planning_context,
                routing_model=routing_model,
            )
            tree = selection.tree
            routing = selection.routing
            cache_key = selection.cache_key
            cache_hit = selection.cache_hit
        else:
            tree = await self._plan_with_context(planning_context)

        self._plan_compilation.normalize_for_submission(
            tree.nodes,
            request,
            preserve_dependencies=bool(routing.get("manifest")),
        )
        if scene == "office" and tree.nodes and not tree.error and not routing.get("manifest"):
            tree = await self._plan_compilation.compile_with_feedback(
                tree,
                routing=routing,
                user_role=user_role,
                context=planning_context,
            )
        if scene == "office":
            routing["input_refs"] = [
                {
                    "doc_id": str(item.get("doc_id") or ""),
                    "filename": str(item.get("filename") or "")[:500],
                    "kind": str(item.get("kind") or "")[:20],
                }
                for item in office_docs or []
                if item.get("doc_id")
            ]

        materialized = await self._materialization.materialize(
            user_id=user_id,
            user_role=user_role,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            submission_key=submission_key,
            tree=tree,
            routing=routing,
        )
        job = materialized.job
        if scene == "office":
            self._plan_contexts[job.job_id] = {
                "request_context": planning_context,
                "user_id": user_id,
                "request": request,
                "scene": scene,
                "project_id": project_id,
                "project_ids": project_ids,
                "llm_api_key": effective_llm.api_key,
                "llm_config": llm_config,
                "clarification_answer": clarification_answer,
                "office_docs": office_docs,
                "prior_summaries": prepared.prior_summaries,
                "presentation_preferences": prepared.presentation_preferences,
            }
            self._llm_configs[job.job_id] = llm_config
            if cache_key and not cache_hit:
                self._pending_plan_cache[job.job_id] = (cache_key, office_docs)

        if materialized.terminal:
            await self._store.create_job(job)
            await job_admission.release(token=admission_token)
            self._discard_pending(job.job_id)
            return job

        await job_admission.promote(admission_token, job.job_id, user_id)
        self._start_heartbeat(job.job_id, user_id)
        try:
            if (
                job.routing.get("manifest")
                and self._temporal_mode
                and self._can_run_manifest_temporal(job)
                and await self._probe_temporal()
            ):
                try:
                    await self._manifest_backend.submit(job, effective_llm.api_key, llm_config)
                    logger.info(
                        "清单任务已提交(Temporal): {} | agent={} request={}",
                        job.job_id[:8],
                        [node.agent for node in job.nodes],
                        request[:40],
                    )
                    return job
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Temporal 清单提交失败，回退自建 DAG: {} | {}",
                        job.job_id[:8],
                        exc,
                    )
                    job.routing = {
                        **(job.routing or {}),
                        "runtime": "legacy",
                        "temporal_submit_error": str(exc)[:200],
                    }

            await self._legacy_backend.submit(job, effective_llm.api_key)
            logger.info(
                "多智能体任务已提交(legacy): {} | agent={} request={}",
                job.job_id[:8],
                [node.agent for node in job.nodes],
                request[:40],
            )
            return job
        except Exception:
            await job_admission.release(job_id=job.job_id, user_id=user_id)
            await self._stop_heartbeat(job.job_id)
            raise
