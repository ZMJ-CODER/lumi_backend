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
import time

from app.agents.orchestration.execution_backend import (
    LegacyDagBackend,
    TemporalManifestBackend,
)
from app.agents.orchestration.execution_loop_service import ExecutionLoopService
from app.agents.orchestration.fork_service import JobForkService
from app.agents.orchestration.failed_job_replan_service import FailedJobReplanService
from app.agents.orchestration.failed_job_recovery_service import FailedJobRecoveryService
from app.agents.orchestration.escalation_service import EscalationService
from app.agents.orchestration.approval_service import ApprovalService
from app.agents.orchestration.control_service import JobControlService
from app.agents.orchestration.admission_lease import AdmissionLeaseMonitor
from app.agents.orchestration.submission_guard import (
    ActiveConversationJobError,
    AgentBackpressureError,
    SubmissionGuard,
    UserJobLimitError,
)
from app.agents.orchestration.job_finalizer import JobFinalizer
from app.agents.orchestration.job_error_service import JobErrorService
from app.agents.orchestration.logical_plan_service import LogicalPlanContinuationService
from app.agents.orchestration.manifest_service import ManifestContinuationService
from app.agents.orchestration.manifest_submission_service import ManifestSubmissionService
from app.agents.orchestration.submission_context_service import SubmissionContextService
from app.agents.orchestration.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.job_materialization_service import JobMaterializationService
from app.agents.orchestration.job_lifecycle_service import JobLifecycleService
from app.agents.orchestration.job_submission_service import JobSubmissionService
from app.agents.orchestration.logical_plan_replan_service import LogicalPlanReplanService
from app.agents.orchestration.replan_evidence_service import ReplanEvidenceService
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.memory_service import OfficeMemoryService
from app.agents.orchestration.query_service import JobQueryService
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.planner import LlmPlanner, Planner
from app.agents.orchestration.plan_context import PlanRequestContext
from app.agents.orchestration.plan_compilation_service import PlanCompilationService
from app.agents.orchestration.plan_cache import PlanCache
from app.agents.orchestration.review import ReviewHook, get_reviewer
from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor
from app.agents.orchestration.state import RedisStateStore
from app.repositories.job_repository import JobRepository, StateStoreJobRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.project_repository import ProjectRepository, SqlAlchemyProjectRepository
from app.agents.orchestration.workers import WORKERS
from app.core.config import settings


class AgentOrchestrator:
    """多智能体协作编排器（单例，全局复用）.

    temporal_enabled：None 时按 settings.AGENT_ORCHESTRATION 决定；
    ``manifest_temporal`` 仅接收显式任务清单，普通办公任务仍走动态 DAG。
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
        plan_cache: PlanCache | None = None,
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
        self._plan_cache = plan_cache or PlanCache()
        self._plan_compilation = PlanCompilationService(
            workers=self._workers,
            plan_with_context=self._plan_with_context,
        )
        self._failed_job_replan = FailedJobReplanService(
            store=self._store,
            workers=self._workers,
            plan_for_level=self._plan_for_level_with_context,
            plan_compilation=self._plan_compilation,
        )
        self._memory = OfficeMemoryService(repository=memory_repository)
        self._manifest_submission = ManifestSubmissionService()
        self._submission_context = SubmissionContextService(memory=self._memory)
        self._office_plan_selection = OfficePlanSelectionService(
            planner=self._planner,
            workers=self._workers,
            assessor=self._complexity_assessor,
            plan_cache=self._plan_cache,
        )
        self._job_materialization = JobMaterializationService(workers=self._workers)
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
        self._temporal_mode = (
            settings.AGENT_ORCHESTRATION == "manifest_temporal"
            if temporal_enabled is None
            else bool(temporal_enabled)
        )
        self._temporal_available = False
        self._temporal_probe_at = 0.0
        self._temporal_unavailable_until = 0.0
        self._runtime = RuntimeGateway(store=self._store, temporal_mode=self._temporal_mode)
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
        # 计划缓存只在任务成功后提交。上下文不进入 Job/API，避免把内部文档 ID
        # 或项目绑定暴露给前端；进程异常时最多损失一次缓存学习，不影响执行正确性。
        self._job_plan_context: dict[str, dict] = {}
        self._pending_plan_cache: dict[str, tuple[str, list[dict] | None]] = {}
        self._manifest_continuation = ManifestContinuationService(
            store=self._store,
            workers=self._workers,
            context_getter=lambda job_id: self._job_plan_context.get(job_id) or {},
        )
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
        self._manifest_backend = TemporalManifestBackend(self._runtime)
        self._submission = JobSubmissionService(
            store=self._store,
            context_service=self._submission_context,
            manifest_submission=self._manifest_submission,
            office_plan_selection=self._office_plan_selection,
            plan_compilation=self._plan_compilation,
            materialization=self._job_materialization,
            temporal_mode=self._temporal_mode,
            can_run_manifest_temporal=self._can_run_manifest_temporal,
            probe_temporal=self._probe_temporal,
            manifest_backend=self._manifest_backend,
            legacy_backend=self._legacy_backend,
            start_heartbeat=self._start_admission_heartbeat,
            stop_heartbeat=self._stop_admission_heartbeat,
            plan_with_context=self._plan_with_context,
            plan_contexts=self._job_plan_context,
            llm_configs=self._job_llm_configs,
            pending_plan_cache=self._pending_plan_cache,
        )
        self._lifecycle = JobLifecycleService(
            plan_cache=self._plan_cache,
            plan_contexts=self._job_plan_context,
            llm_configs=self._job_llm_configs,
            pending_plan_cache=self._pending_plan_cache,
        )
        self._finalizer = JobFinalizer(
            stop_heartbeat=lambda job_id: self._stop_admission_heartbeat(job_id),
            on_summary=lambda job: self._record_office_summary(job),
            on_task_index=lambda job: self._record_office_task_index(job),
            on_metric=self._lifecycle.record_metric,
            on_learning=self._lifecycle.learn_from_finished_job,
            on_terminal=self._lifecycle.cleanup_terminal,
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
            continue_manifest=self._continue_manifest_job,
            continue_logical_plan=self._continue_logical_plan,
            maybe_replan=self._maybe_replan_failed_job,
            node_concurrency=settings.AGENT_NODE_CONCURRENCY,
        )
        self._control = JobControlService(
            repository=self._store,
            approval=self._approval,
            temporal_backend=self._manifest_backend,
            legacy_backend=self._legacy_backend,
            finalizer=self._finalizer,
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
            on_learning=self._lifecycle.learn_from_finished_job,
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
        user_role: str = "user",
    ) -> Job:
        """规划任务树并启动执行（Temporal 优先），立即返回 Job."""
        submission_material = {
            "request": request,
            "scene": scene,
            "conversation_id": conversation_id,
            "project_id": project_id,
            "project_ids": sorted(str(x) for x in (project_ids or [])),
            "office_docs": sorted(
                str(d.get("doc_id")) for d in (office_docs or []) if d.get("doc_id")
            ),
            "clarification_answer": clarification_answer,
        }
        submission_key = hashlib.sha256(
            json.dumps(submission_material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Submission admission is owned by SubmissionGuard.
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
                user_role=user_role,
                submission_key=submission_key,
                admission_token=admission_token,
            ),
        )

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
        user_role: str,
        submission_key: str,
        admission_token: str,
    ) -> Job:
        """Delegate one admitted submission to the focused transaction service."""
        return await self._submission.submit(
            user_id=user_id,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            project_id=project_id,
            project_ids=project_ids,
            llm_api_key=llm_api_key,
            clarification_answer=clarification_answer,
            office_docs=office_docs,
            user_role=user_role,
            submission_key=submission_key,
            admission_token=admission_token,
        )

    @staticmethod
    def _can_run_manifest_temporal(job: Job) -> bool:
        return RuntimeGateway.can_run_manifest(job)

    async def _submit_temporal(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        await self._runtime.submit_static(job, llm_api_key, llm_config)

    async def _submit_manifest_temporal(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        await self._runtime.submit_manifest(job, llm_api_key, llm_config)

    @staticmethod
    def _is_manifest_temporal_job(job: Job | None) -> bool:
        return RuntimeGateway.is_manifest_job(job)

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

    async def _continue_manifest_job(self, job: Job) -> bool:
        """Commit a manifest batch through the dedicated continuation service."""
        return await self._manifest_continuation.continue_job(job)


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

    async def _learn_from_finished_job(self, job: Job) -> None:
        """Compatibility delegate for lifecycle plan learning."""
        await self._lifecycle.learn_from_finished_job(job)

    def _discard_pending_learning(self, job_id: str) -> None:
        self._lifecycle.discard_pending_learning(job_id)

    async def _attach_progress(self, job: Job) -> Job:
        """Compatibility delegate for response-only progress decoration."""
        return await self._lifecycle.attach_progress(job)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        return await self._query.list_jobs(user_id, limit)

    async def admin_list_jobs(self, limit: int = 50) -> list[Job]:
        return await self._query.admin_list_jobs(limit)

    # ── 控制：终止 / 暂停 / 恢复 ─────────────────────────────

    async def cancel_job(self, job_id: str, keep_completed: bool = True) -> Job | None:
        return await self._control.cancel(job_id, keep_completed)

    async def approve_job(self, job_id: str, node_id: str, approved: bool = True) -> None:
        await self._control.approve(job_id, node_id, approved)

    async def pause_job(self, job_id: str) -> Job | None:
        return await self._control.pause(job_id)

    async def resume_job(self, job_id: str) -> Job | None:
        return await self._control.resume(job_id)


# 全局单例（API 层使用）
orchestrator = AgentOrchestrator()
