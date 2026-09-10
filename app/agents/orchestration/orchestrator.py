"""多智能体编排器 —— 提交任务 → 规划 → Temporal Workflow 执行 → 状态查询.

对外能力（API 契约不变）：
  - submit_job: 规划任务树 → 启动 Temporal Workflow（失败自动回退自建 DAG）
  - get_job / list_jobs: 查询状态（优先查询 Temporal，回退 Redis 快照）
  - cancel_job: 用户终止（signal 携带 keep_completed）
  - pause_job / resume_job: 暂停/恢复调度（signal）

状态由 Temporal 管理（Workflow query 返回 Job 快照）；Redis 仅保留
引导快照 + 用户任务索引（list_jobs 分页），以及 BYOK key 的短 TTL 临时桥接。
"""

import asyncio
import hashlib
import json
import uuid

from loguru import logger

from app.agents.orchestration.backends.legacy import LegacyDagBackend
from app.agents.orchestration.backends.temporal_logical_effects import TemporalLogicalEffectsBackend
from app.agents.orchestration.backends.temporal_logical_read import TemporalLogicalReadBackend
from app.agents.orchestration.backends.temporal_static import TemporalStaticBackend
from app.agents.orchestration.execution_loop_service import ExecutionLoopService
from app.agents.orchestration.fork_service import JobForkService
from app.agents.orchestration.failed_job_replan_service import FailedJobReplanService
from app.agents.orchestration.failed_job_recovery_service import FailedJobRecoveryService
from app.agents.orchestration.escalation_service import EscalationService
from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.control_service import JobControlService
from app.agents.orchestration.admission_lease import AdmissionLeaseMonitor
from app.agents.orchestration.admission import AdmissionBackpressureError, job_admission
from app.agents.orchestration.submission_guard import (
    ActiveConversationJobError,
    AgentBackpressureError,
    SubmissionGuard,
    UserJobLimitError,
)
from app.agents.orchestration.job_finalizer import JobFinalizer
from app.agents.orchestration.job_error_service import JobErrorService
from app.agents.orchestration.logical_plan_service import LogicalPlanContinuationService
from app.agents.orchestration.submission_context_service import SubmissionContextService
from app.agents.orchestration.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.job_materialization_service import JobMaterializationService
from app.agents.orchestration.job_lifecycle_service import JobLifecycleService
from app.agents.orchestration.job_submission_service import JobSubmissionService
from app.agents.orchestration.job_coordinator import JobOperationsCoordinator
from app.agents.orchestration.logical_plan_replan_service import LogicalPlanReplanService
from app.agents.orchestration.scheduling.service import PlanPatchAppendResult, PlanPatchScheduler
from app.agents.orchestration.replan_evidence_service import ReplanEvidenceService
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.memory_service import OfficeMemoryService
from app.agents.orchestration.query_service import JobQueryService
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.planner import LlmPlanner, Planner
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.compilation import PlanCompilationService
from app.agents.orchestration.review import ReviewHook, get_reviewer
from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor
from app.agents.orchestration.state import RedisStateStore
from app.repositories.job_repository import JobRepository, StateStoreJobRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.project_repository import ProjectRepository, SqlAlchemyProjectRepository
from app.agents.orchestration.workers import WORKERS
from app.core.config import settings

# Kept as module-level compatibility exports for API callers and older tests;
# the orchestrator itself does not need to reference these exception classes.
__all__ = [
    "AgentOrchestrator",
    "ActiveConversationJobError",
    "AgentBackpressureError",
    "UserJobLimitError",
]


class AgentOrchestrator:
    """多智能体协作编排器（单例，全局复用）.

    temporal_enabled：None 时按 settings.AGENT_ORCHESTRATION 决定；
    ``temporal`` 只接收通过运行时准入的静态 DAG；其余办公任务走受控动态 DAG。
    测试/显式场景可传 False 强制走自建 DAG（legacy）。
    """

    def __init__(
        self,
        store: JobRepository | None = None,
        planner: Planner | None = None,
        workers: dict | None = None,
        review: ReviewHook | None = None,
        temporal_enabled: bool | None = None,
        complexity_assessor: TaskComplexityAssessor | None = None,
        job_repository: JobRepository | None = None,
        project_repository: ProjectRepository | None = None,
        memory_repository: MemoryRepository | None = None,
    ):
        base_store = store or RedisStateStore()
        # Keep the legacy ``_store`` name as an internal compatibility alias,
        # but make every orchestration dependency receive the repository
        # boundary.  A fake repository can therefore replace Redis in tests.
        self._job_repository = job_repository or StateStoreJobRepository(base_store)
        self._store = self._job_repository
        self._project_repository = project_repository or SqlAlchemyProjectRepository()
        self._planner = planner or LlmPlanner(project_repository=self._project_repository)
        self._workers = workers if workers is not None else WORKERS
        self._review = review or get_reviewer()
        self._complexity_assessor = complexity_assessor or TaskComplexityAssessor()
        self._plan_compilation = PlanCompilationService(
            workers=self._workers,
            plan_with_context=self._plan_with_context,
            temporal_static_mode=(
                (settings.AGENT_ORCHESTRATION.strip().lower() == "temporal")
                if temporal_enabled is None
                else bool(temporal_enabled)
            ),
        )
        self._failed_job_replan = FailedJobReplanService(
            store=self._store,
            workers=self._workers,
            plan_for_level=self._plan_for_level_with_context,
            plan_compilation=self._plan_compilation,
        )
        self._memory = OfficeMemoryService(repository=memory_repository)
        self._submission_context = SubmissionContextService(memory=self._memory)
        self._office_plan_selection = OfficePlanSelectionService(
            planner=self._planner,
            workers=self._workers,
            assessor=self._complexity_assessor,
        )
        self._replan_evidence = ReplanEvidenceService()
        self._logical_plan_replan = LogicalPlanReplanService(
            store=self._store,
            workers=self._workers,
            plan_for_level=self._plan_for_level_with_context,
            plan_compilation=self._plan_compilation,
            evidence=self._replan_evidence,
        )
        self._job_errors = JobErrorService(store=self._store)
        self._approval = ApprovalService(store=self._store)
        self._logical_plan = LogicalPlanContinuationService(store=self._store)
        self._escalation = EscalationService(store=self._store)
        self._failed_job_recovery = FailedJobRecoveryService(
            store=self._store,
            failed_replan=self._failed_job_replan,
            replan_logical_plan=self._maybe_replan_logical_plan,
            handle_escalation=self._handle_task_escalation,
            terminal_model_failure=self._has_terminal_model_failure,
            context_getter=lambda job_id: self._job_plan_context.get(job_id),
            planner_level_aware=lambda: callable(getattr(self._planner, "plan_for_level", None)),
            dynamic_enabled=lambda: settings.AGENT_DYNAMIC_SUBGRAPH_ENABLED,
            max_replans=lambda: settings.AGENT_SUBGRAPH_MAX_REPLANS,
        )
        # ── Temporal 模式 ──
        self._orchestration_mode = (
            settings.AGENT_ORCHESTRATION.strip().lower()
            if temporal_enabled is None
            else ("temporal" if temporal_enabled else "legacy")
        )
        self._temporal_mode = self._orchestration_mode == "temporal"
        self._temporal_static_mode = self._orchestration_mode == "temporal"
        self._temporal_logical_read_mode = (
            self._orchestration_mode == "temporal" and settings.TEMPORAL_LOGICAL_READ_ENABLED
        )
        self._temporal_logical_effects_mode = (
            self._orchestration_mode == "temporal" and settings.TEMPORAL_LOGICAL_EFFECTS_ENABLED
        )
        self._temporal_available = False
        self._temporal_probe_at = 0.0
        self._temporal_unavailable_until = 0.0
        self._runtime = RuntimeGateway(store=self._store, temporal_mode=self._temporal_mode)
        self._job_materialization = JobMaterializationService(
            workers=self._workers,
            temporal_static_mode=self._temporal_static_mode,
        )
        # ── legacy 自建 DAG 后台任务 ──
        self._tasks: dict[str, asyncio.Task] = {}  # job_id -> 后台执行任务
        # BYOK：legacy 路径的任务内临时 API key（仅内存，任务结束即释放）
        self._job_api_keys: dict[str, str] = {}
        self._job_llm_configs: dict[str, dict] = {}
        # legacy DAG 在 API 进程内执行。Redis 短暂不可读时保留运行中快照，
        # 防止前端已收到 job_id 却立即被 404；Redis 恢复后仍优先使用其快照。
        self._live_jobs: dict[str, Job] = {}
        # 同进程内串行化同一用户的“检查并提交”，避免两个并发请求同时越过限流检查。
        self._submission_guard = SubmissionGuard(store=self._store)
        # 规划上下文不进入 Job/API，避免把内部文档 ID 或项目绑定暴露给前端。
        self._job_plan_context: dict[str, dict] = {}
        self._lease_monitor = AdmissionLeaseMonitor(
            store=self._store,
            tasks=self._tasks,
            error_service=self._job_errors,
        )
        self._legacy_backend = LegacyDagBackend(
            store=self._store,
            live_jobs=self._live_jobs,
            tasks=self._tasks,
            api_keys=self._job_api_keys,
            run_job=self._run_job,
        )
        self._static_backend = TemporalStaticBackend(self._runtime)
        self._logical_read_backend = TemporalLogicalReadBackend(self._runtime)
        self._logical_effects_backend = TemporalLogicalEffectsBackend(self._runtime)
        self._plan_patches = PlanPatchScheduler(store=self._store, workers=self._workers)
        self._submission = JobSubmissionService(
            store=self._store,
            context_service=self._submission_context,
            office_plan_selection=self._office_plan_selection,
            plan_compilation=self._plan_compilation,
            materialization=self._job_materialization,
            temporal_static_mode=self._temporal_static_mode,
            temporal_logical_read_mode=self._temporal_logical_read_mode,
            temporal_logical_effects_mode=self._temporal_logical_effects_mode,
            can_run_static_temporal=self._can_run_static_temporal,
            probe_temporal=self._probe_temporal,
            static_backend=self._static_backend,
            logical_read_backend=self._logical_read_backend,
            logical_effects_backend=self._logical_effects_backend,
            legacy_backend=self._legacy_backend,
            start_heartbeat=self._start_admission_heartbeat,
            stop_heartbeat=self._stop_admission_heartbeat,
            plan_with_context=self._plan_with_context,
            plan_contexts=self._job_plan_context,
            llm_configs=self._job_llm_configs,
        )
        self._lifecycle = JobLifecycleService(
            plan_contexts=self._job_plan_context,
            llm_configs=self._job_llm_configs,
        )
        self._finalizer = JobFinalizer(
            stop_heartbeat=lambda job_id: self._stop_admission_heartbeat(job_id),
            on_summary=lambda job: self._record_office_summary(job),
            on_task_index=lambda job: self._record_office_task_index(job),
            on_metric=self._lifecycle.record_metric,
            on_learning=self._lifecycle.finalize_plan,
            on_terminal=self._lifecycle.cleanup_terminal,
        )
        from app.agents.orchestration.execution.service import ApplicationTaskExecutionService

        task_execution_service = ApplicationTaskExecutionService(
            store=self._store,
            workers=self._workers,
            review=self._review,
        )
        self._execution_loop = ExecutionLoopService(
            store=self._store,
            workers=self._workers,
            review=self._review,
            job_errors=self._job_errors,
            finalizer=self._finalizer,
            live_jobs=self._live_jobs,
            tasks=self._tasks,
            api_keys=self._job_api_keys,
            llm_configs=self._job_llm_configs,
            plan_context=self._job_plan_context,
            context_getter=lambda job_id: self._job_plan_context.get(job_id) or {},
            continue_logical_plan=self._continue_logical_plan,
            maybe_replan=self._maybe_replan_failed_job,
            node_concurrency=settings.AGENT_NODE_CONCURRENCY,
            suspend_capacity=self._finalizer.suspend_capacity,
            ensure_active_capacity=self._ensure_active_capacity,
            task_execution_service=task_execution_service,
        )
        # step_confirm 计划优先任务的单步执行器（/jobs/{id}/resume action=run_next）。
        from app.agents.orchestration.step_run_service import StepRunService

        self._step_run = StepRunService(
            store=self._store,
            workers=self._workers,
            review=self._review,
            finalizer=self._finalizer,
            llm_configs=self._job_llm_configs,
            ensure_active_capacity=self._ensure_active_capacity,
            suspend_capacity=self._finalizer.suspend_capacity,
            handle_escalation=self._handle_task_escalation,
            # 最后一步完成后：由执行循环做终态合成与记录（引擎对已全完成的
            # 任务为零执行）；失败则先记录升级建议，再由 finalizer 终态清理。
            finalize_completed=lambda job: self._execution_loop.run(job.job_id),
            finalize_failed=self._finalize_step_failed,
        )
        self._control = JobControlService(
            repository=self._store,
            approval=self._approval,
            static_backend=self._static_backend,
            logical_read_backend=self._logical_read_backend,
            logical_effects_backend=self._logical_effects_backend,
            legacy_backend=self._legacy_backend,
            finalizer=self._finalizer,
            ensure_active_capacity=self._ensure_active_capacity,
        )
        self._operations = JobOperationsCoordinator(
            submission=self._submission,
            lifecycle=self._lifecycle,
            control=self._control,
        )
        self._fork = JobForkService(
            repository=self._store,
            workers=self._workers,
            list_jobs=lambda user_id, limit: self.list_jobs(user_id, limit),
            start_heartbeat=self._start_admission_heartbeat,
            live_jobs=self._live_jobs,
            tasks=self._tasks,
            api_keys=self._job_api_keys,
            llm_configs=self._job_llm_configs,
            plan_context=self._job_plan_context,
            run_job=self._run_job,
        )
        self._query = JobQueryService(
            store=self._store,
            live_jobs=self._live_jobs,
            probe_temporal=lambda: self._probe_temporal(),
            stop_heartbeat=lambda job_id: self._stop_admission_heartbeat(job_id),
            on_summary=lambda job: self._record_office_summary(job),
            on_task_index=lambda job: self._record_office_task_index(job),
            on_metric=self._lifecycle.record_metric,
            on_learning=self._lifecycle.finalize_plan,
            attach_progress=self._lifecycle.attach_progress,
            on_terminal=self._lifecycle.cleanup_terminal,
            finalizer=self._finalizer,
        )

    async def _plan_with_context(self, context: PlanRequestContext):
        """Invoke a planner through the context boundary with legacy fallback."""
        method = getattr(self._planner, "plan_context", None)
        if callable(method):
            return await method(context)
        return await self._planner.plan(*context.as_legacy_args())

    async def _plan_for_level_with_context(
        self,
        level: ComplexityLevel,
        context: PlanRequestContext,
        *,
        bypass_fast_paths: bool = False,
    ):
        """Level-aware counterpart that keeps custom Planner signatures intact."""
        method = getattr(self._planner, "plan_for_level", None)
        if not callable(method):
            return await self._plan_with_context(context)
        if getattr(self._planner, "supports_context_planning", False):
            return await method(level, context=context, bypass_fast_paths=bypass_fast_paths)
        return await method(
            level,
            *context.as_legacy_args(),
            bypass_fast_paths=bypass_fast_paths,
        )

    def _start_admission_heartbeat(self, job_id: str, user_id: str) -> None:
        self._lease_monitor.start(job_id, user_id)

    async def _stop_admission_heartbeat(self, job_id: str) -> None:
        await self._lease_monitor.stop(job_id)

    async def _ensure_active_capacity(self, job: Job) -> bool:
        """Re-admit a suspended job before it may execute another node."""
        try:
            await job_admission.activate(job.job_id, job.user_id)
        except AdmissionBackpressureError:
            return False
        job.routing = dict(job.routing or {})
        job.routing.pop("admission_released_while_waiting", None)
        self._start_admission_heartbeat(job.job_id, job.user_id)
        await self._store.save_job(job)
        return True

    # Compatibility delegates. Persistence lives in MemoryRepository and
    # attachment filtering remains in OfficeMemoryService; keeping these names
    # avoids breaking API adapters and existing extensions.
    async def _load_office_summaries(self, conversation_id: str) -> str:
        return await self._memory.load_summaries(conversation_id)

    async def _record_office_summary(self, job: Job) -> None:
        await self._memory.record_summary(job)

    async def _record_office_task_index(self, job: Job) -> None:
        await self._memory.record_task_index(job)

    async def _load_office_recall_context(
        self, user_id: str, request: str, conversation_id: str | None
    ) -> str:
        return await self._memory.load_recall_context(user_id, request, conversation_id)

    async def _load_office_presentation_preferences(self, user_id: str) -> str:
        return await self._memory.load_presentation_preferences(user_id)

    async def _verified_office_docs(
        self, user_id: str, request: str, office_docs: list[dict] | None
    ) -> list[dict]:
        return await self._memory.verify_documents(user_id, request, office_docs)

    async def _record_job_metric(self, job: Job) -> None:
        """Compatibility delegate for lifecycle telemetry."""
        await self._lifecycle.record_metric(job)

    # ── Temporal 可用性探测（成功缓存；失败 30s 后重试）──────────

    async def _probe_temporal(self) -> bool:
        result = await self._runtime.probe_temporal()
        # Keep the legacy health-check attributes readable for existing admin
        # endpoints and extensions during the migration window.
        self._temporal_available = self._runtime.temporal_available
        self._temporal_probe_at = self._runtime.temporal_probe_at
        self._temporal_unavailable_until = self._runtime.temporal_unavailable_until
        return result

    # ── 提交与执行 ───────────────────────────────────────────

    async def submit_job(
        self,
        user_id: str,
        request: str,
        scene: str = "office",
        conversation_id: str | None = None,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        workspace_id: str | None = None,
        user_role: str = "user",
        execution_preference: str = "use_workspace_policy",
    ) -> Job:
        """规划任务树并启动执行（Temporal 优先），立即返回 Job."""
        if scene == "office" and not workspace_id and conversation_id:
            try:
                from app.services.workspaces import workspace_for_conversation

                bound = workspace_for_conversation(user_id, conversation_id)
                workspace_id = str((bound or {}).get("workspace_id") or "") or None
            except (LookupError, ValueError, OSError):
                workspace_id = None
        submission_material = {
            "request": request,
            "scene": scene,
            "conversation_id": conversation_id,
            "project_id": project_id,
            "project_ids": sorted(str(x) for x in (project_ids or [])),
            "office_docs": sorted(
                str(d.get("doc_id")) for d in (office_docs or []) if d.get("doc_id")
            ),
            "workspace_id": str(workspace_id or ""),
            "clarification_answer": clarification_answer,
            "execution_preference": str(execution_preference or ""),
        }
        submission_key = hashlib.sha256(
            json.dumps(submission_material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Submission admission is owned by SubmissionGuard.
        try:
            return await self._submission_guard.submit(
                user_id=user_id,
                conversation_id=conversation_id,
                submission_key=submission_key,
                create_job=lambda admission_token: self._submit_job_unlocked(
                    user_id=user_id,
                    request=request,
                    scene=scene,
                    conversation_id=conversation_id,
                    project_id=project_id,
                    project_ids=project_ids,
                    llm_api_key=llm_api_key,
                    clarification_answer=clarification_answer,
                    office_docs=office_docs,
                    workspace_id=workspace_id,
                    user_role=user_role,
                    execution_preference=execution_preference,
                    submission_key=submission_key,
                    admission_token=admission_token,
                ),
            )
        except Exception as exc:
            # Admission is a public control-flow outcome, not a failed job.
            # API/SSE callers must receive the existing 429 contract instead
            # of a synthetic terminal snapshot that silently frees capacity.
            if isinstance(exc, (ActiveConversationJobError, UserJobLimitError, AgentBackpressureError)):
                raise
            # 规划/物化发生在 Job 创建前，统一生成可查询的失败快照，避免
            # 对话接口因内部异常直接返回 500。
            from app.core.error_mapping import map_task_error
            from app.agents.orchestration.models import JobStatus

            public = map_task_error(exc)
            failed = Job(
                job_id=str(uuid.uuid4()), user_id=user_id, user_role=user_role,
                request=request, scene=scene, conversation_id=conversation_id,
                submission_key=submission_key, status=JobStatus.FAILED,
                error=public.message,
                result={"type": "execution_error", "status": "failed", "error_code": public.code, "message": public.message, "retryable": public.retryable},
                routing={"error_code": public.code},
            )
            await self._store.create_job(failed)
            return failed

    async def preview_plan(
        self,
        user_id: str,
        request: str,
        scene: str = "office",
        conversation_id: str | None = None,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        workspace_id: str | None = None,
        user_role: str = "user",
    ) -> tuple[object, dict]:
        """Build and compile an office plan without creating or scheduling a Job.

        This is intentionally a control-plane operation: it resolves the same
        authorized context, model snapshot, capability candidates and compiler
        feedback loop as submission, but never acquires admission, writes job
        state, creates an effect intent, or hands a node to an executor.
        """
        if scene == "office" and not workspace_id and conversation_id:
            try:
                from app.services.workspaces import workspace_for_conversation

                bound = workspace_for_conversation(user_id, conversation_id)
                workspace_id = str((bound or {}).get("workspace_id") or "") or None
            except (LookupError, ValueError, OSError):
                workspace_id = None
        prepared = await self._submission_context.prepare(
            user_id=user_id,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            project_id=project_id,
            project_ids=project_ids,
            request_api_key=llm_api_key,
            clarification_answer=clarification_answer,
            office_docs=office_docs,
            workspace_id=workspace_id,
        )
        routing_model = prepared.effective_llm.public_dict()
        if scene == "office":
            selection = await self._office_plan_selection.select(
                user_id=user_id,
                request=request,
                user_role=user_role,
                project_id=project_id,
                project_ids=project_ids,
                clarification_answer=clarification_answer,
                office_docs=prepared.office_docs,
                prior_summaries=prepared.prior_summaries,
                planning_context=prepared.planning_context,
                routing_model=routing_model,
            )
            tree, routing = selection.tree, selection.routing
        else:
            tree = await self._plan_with_context(prepared.planning_context)
            routing = {"llm": routing_model}

        self._plan_compilation.normalize_for_submission(
            tree.nodes,
            request,
            preserve_dependencies=True,
            complexity_level=str(routing.get("level") or ""),
        )
        if scene == "office" and tree.nodes and not tree.error:
            tree = await self._plan_compilation.compile_with_feedback(
                tree,
                routing=routing,
                user_role=user_role,
                context=prepared.planning_context,
            )
        routing["preview"] = True
        routing["workspace_id"] = str(workspace_id or "") or None
        routing["input_ref_count"] = len(prepared.office_docs)
        return tree, routing

    async def _submit_job_unlocked(
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
        workspace_id: str | None,
        user_role: str,
        submission_key: str,
        admission_token: str,
        execution_preference: str = "use_workspace_policy",
    ) -> Job:
        """Delegate one admitted submission to the focused transaction service."""
        return await self._operations.submit(
            user_id=user_id,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            project_id=project_id,
            project_ids=project_ids,
            llm_api_key=llm_api_key,
            clarification_answer=clarification_answer,
            office_docs=office_docs,
            workspace_id=workspace_id,
            user_role=user_role,
            execution_preference=execution_preference,
            submission_key=submission_key,
            admission_token=admission_token,
        )

    @staticmethod
    def _can_run_static_temporal(job: Job) -> bool:
        return RuntimeGateway.can_run_static(job)

    async def _submit_temporal(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        await self._runtime.submit_static(job, llm_api_key, llm_config)

    async def fork_job(
        self,
        job_id: str,
        *,
        node_id: str,
        params: dict | None = None,
        instruction: str | None = None,
        llm_api_key: str | None = None,
    ) -> Job:
        """Create an immutable execution branch through the fork service."""
        return await self._fork.fork(
            job_id,
            node_id=node_id,
            params=params,
            instruction=instruction,
            llm_api_key=llm_api_key,
        )
    async def _run_job(self, job_id: str) -> None:
        """Delegate the legacy run lifecycle to the execution-loop service."""
        await self._execution_loop.run(job_id)

    @staticmethod
    def _has_terminal_model_failure(job: Job) -> bool:
        """Billing/auth/provider failures terminate the snapshot-bound job."""
        terminal = {
            "MODEL_INSUFFICIENT_BALANCE", "MODEL_AUTH_ERROR", "MODEL_NOT_FOUND",
            "MODEL_CONFIG_ERROR", "MODEL_TOOL_CALL_UNSUPPORTED",
            "MODEL_PROVIDER_UNAVAILABLE", "MODEL_CONNECTION_ERROR", "MODEL_UNAVAILABLE",
        }
        failed = [node for node in (job.nodes or []) if node.status == TaskStatus.FAILED]
        if not any(str(node.error_code or "").upper() in terminal for node in failed):
            return False
        job.status = JobStatus.FAILED
        job.error = next(
            (str(node.error or "模型连接异常，办公任务已停止") for node in failed
             if str(node.error_code or "").upper() in terminal),
            "模型连接异常，办公任务已停止。请检查模型连接、API Key、账户余额或供应商状态后重试。",
        )
        return True

    async def _continue_logical_plan(self, job: Job) -> bool:
        """Commit a single ordinary-DAG frontier and materialize the next one."""
        return await self._logical_plan.continue_job(job)

    async def _maybe_replan_logical_plan(self, job: Job, llm_api_key: str | None) -> bool:
        """Apply approval safety controls, then delegate safe L3 recovery."""
        pointer = (job.routing or {}).get("logical_plan")
        if not isinstance(pointer, dict) or not pointer.get("plan_id"):
            return False
        if job.scene != "office" or job.status in {
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
        }:
            return False
        if self._has_terminal_model_failure(job):
            await self._store.save_job(job)
            return False

        # L2 stays in the prebuilt approval/clarification controller.  An
        # approved node remains the same logical node, so reopen only its
        # materialized record for the later retry instead of changing the
        # logical graph or consuming another L3 attempt.
        if await self._handle_task_escalation(job):
            if job.status == JobStatus.WAITING_APPROVAL:
                from app.agents.orchestration.logical_plan import (
                    load_logical_plan,
                    logical_plan_progress,
                    save_logical_plan,
                )

                plan = await load_logical_plan(job.user_id, str(pointer["plan_id"]))
                if not plan:
                    job.status = JobStatus.FAILED
                    job.error = "逻辑计划状态不可用，无法安全恢复失败步骤。"
                    await self._store.save_job(job)
                    return False
                records = plan.get("nodes") or {}
                for node in job.nodes:
                    logical_id = str((node.metadata or {}).get("logical_node_id") or node.id)
                    record = records.get(logical_id)
                    if isinstance(record, dict) and node.status == TaskStatus.PENDING:
                        record["status"] = "materialized"
                        record["error"] = ""
                        record["error_code"] = ""
                job.routing = dict(job.routing or {})
                job.routing["logical_plan"] = {
                    **pointer,
                    "revision": plan.get("revision", 1),
                    "progress": logical_plan_progress(plan),
                }
                await save_logical_plan(job.user_id, plan)
                await self._store.save_job(job)
            return True

        context = self._job_plan_context.get(job.job_id)
        return await self._logical_plan_replan.replan(
            job,
            context=context,
            llm_api_key=llm_api_key,
            dynamic_enabled=settings.AGENT_DYNAMIC_SUBGRAPH_ENABLED,
            max_replans=settings.AGENT_SUBGRAPH_MAX_REPLANS,
            planner_level_aware=callable(getattr(self._planner, "plan_for_level", None)),
        )

    async def _maybe_replan_failed_job(self, job: Job, llm_api_key: str | None) -> bool:
        """Delegate ordinary failure recovery to the policy coordinator."""
        return await self._failed_job_recovery.maybe_recover(job, llm_api_key)

    async def _handle_task_escalation(self, job: Job) -> bool:
        """Resolve L2 signals through deterministic orchestration controls.

        Missing prerequisites become a stable clarification result. Confirmation
        signals make one existing node wait for the API approval flow.  Neither
        branch permits an arbitrary new edge/node supplied by a worker.
        """
        return await self._escalation.handle_task_escalation(job)

    # ── 查询 ────────────────────────────────────────────────

    async def get_job(self, job_id: str) -> Job | None:
        """Read a job through the dedicated query service."""
        job = await self._query.get_job(job_id)
        if job is None:
            return None
        return job

    async def _finalize_plan(self, job: Job) -> None:
        """Finalize plan telemetry without caching executable workflow graphs."""
        await self._operations.finalize_plan(job)

    def _discard_pending_learning(self, job_id: str) -> None:
        self._operations.discard_pending_learning(job_id)

    async def _attach_progress(self, job: Job) -> Job:
        """Compatibility delegate for response-only progress decoration."""
        return await self._operations.attach_progress(job)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        return await self._query.list_jobs(user_id, limit)

    async def admin_list_jobs(self, limit: int = 50) -> list[Job]:
        return await self._query.admin_list_jobs(limit)

    # ── 控制：终止 / 暂停 / 恢复 ─────────────────────────────

    async def cancel_job(self, job_id: str, keep_completed: bool = True) -> Job | None:
        return await self._operations.cancel(job_id, keep_completed)

    async def approve_job(self, job_id: str, node_id: str, approved: bool = True) -> None:
        await self._operations.approve(job_id, node_id, approved)

    async def pause_job(self, job_id: str) -> Job | None:
        return await self._operations.pause(job_id)

    async def resume_job(self, job_id: str) -> Job | None:
        return await self._operations.resume(job_id)

    async def _finalize_step_failed(self, job: Job) -> None:
        """单步执行失败：先按升级决策树记录建议，再做终态清理。"""
        try:
            from app.services.upgrade_adapter import suggest_upgrade

            result = job.result if isinstance(job.result, dict) else {}
            error_code = str(
                result.get("error_code")
                or next((node.error_code for node in job.nodes if node.error_code), "")
                or ""
            )
            current = str((job.routing or {}).get("complexity") or "M1")
            suggestion = suggest_upgrade(error_code, current=current)
            if suggestion:
                job.routing = {**(job.routing or {}), "upgrade": suggestion}
                await self._store.save_job(job)
        except Exception as exc:  # noqa: BLE001 - 建议记录失败不影响终态清理
            logger.warning("记录升级建议失败 {}: {}", str(job.job_id)[:12], str(exc)[:160])
        await self._finalizer.finalize(job)

    async def stream_run_next(
        self,
        *,
        job_id: str,
        expected_step_id: str = "",
        plan_revision: int | None = None,
        idempotency_key: str = "",
        workspace_bound: bool = True,
    ):
        """step_confirm 计划优先任务：执行“下一步”并产出 SSE 事件流。

        事件类型：step_started / delta / step_completed(+waiting_next 载荷) /
        waiting_approval / task_completed / task_failed / error / view。
        """
        async for event in self._step_run.run_next_stream(
            job_id=job_id,
            expected_step_id=expected_step_id,
            plan_revision=plan_revision,
            idempotency_key=idempotency_key,
            workspace_bound=workspace_bound,
        ):
            yield event

    async def append_plan_patch(self, job_id: str, user_id: str, patch) -> PlanPatchAppendResult:
        """持久化受限补图，并在需要时恢复等待中的 Legacy 执行器。"""
        outcome = await self._plan_patches.append_external(
            job_id=job_id,
            user_id=user_id,
            patch=patch,
        )
        if outcome.requires_legacy_resume:
            resumed = await self.resume_job(job_id)
            if resumed is None:
                raise RuntimeError("补图已保存，但 Legacy 执行器恢复失败")
            return PlanPatchAppendResult(
                job=resumed,
                patch_id=outcome.patch_id,
                slot_id=outcome.slot_id,
                revision=outcome.revision,
                replayed=outcome.replayed,
                temporal_signaled=False,
                requires_legacy_resume=False,
            )
        return outcome


# 全局单例（API 层使用）
orchestrator = AgentOrchestrator()
