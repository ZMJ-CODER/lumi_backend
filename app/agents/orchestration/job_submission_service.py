"""办公任务的规划、物化与运行时提交。

Admission locking stays at the orchestrator boundary.  Once that lock is held,
this service owns the linear submission transaction: prepare context, choose a
plan, materialize a job, promote capacity, and select the runtime backend.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.orchestration.job_materialization_service import JobMaterializationService
from app.agents.orchestration.capability_preflight_service import attach_capability_resolution_time
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.planning.compilation import PlanCompilationService
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.submission_context_service import SubmissionContextService
from app.agents.orchestration.admission import job_admission
from app.repositories.job_repository import JobRepository
from app.core.error_mapping import map_task_error

#: 附件 kind → 需要视觉能力（模型路由的能力需求；与文档解析器的 kind 词表一致）。
_IMAGE_DOC_KINDS: frozenset[str] = frozenset(
    {"image", "img", "picture", "photo", "screenshot", "vision"}
)


def _capability_requirements(*, scene: str, request: str, office_docs: list[dict] | None):
    """任务 → 模型能力需求（**只在** ``MODEL_CAPABILITY_ROUTER_V2`` 打开时调用）。

    只使用服务端已有的事实来源：入口副作用判定（``capability_preflight_facts``）回答
    "这次任务是否真的会用到工具"，附件 kind 回答"是否需要视觉"；判定失败时按保守
    方式（office 场景视为需要工具）处理，绝不因为探测故障放过能力缺口。
    """
    from app.core.model_capability_router import CapabilityRequirements

    try:
        from app.agents.orchestration.task_preflight import capability_preflight_facts

        facts = capability_preflight_facts(request, office_docs=office_docs)
        needs_tools = bool((facts.get("profile") or {}).get("action_intents"))
    except Exception as exc:  # noqa: BLE001 - 事实缺失按保守处理
        logger.debug("能力需求判定降级（按场景保守取值）: {}", str(exc)[:120])
        needs_tools = scene == "office"
    kinds = {str((item or {}).get("kind") or "").casefold() for item in (office_docs or [])}
    return CapabilityRequirements(
        needs_tools=needs_tools,
        needs_vision=bool(kinds & _IMAGE_DOC_KINDS),
        needs_streaming=scene in {"office", "chat"},
    )


def _profile_action_intents(routed: object) -> tuple[str, ...]:
    """Router v2 决策 → 动作意图（策略层消费画像事实的唯一读法）。

    没有 Router v2 决策时返回空元组：策略层退回旧 reasons 兜底，**不自己解析**原文。
    """
    if routed is None:
        return ()
    profile = getattr(routed, "profile", None)
    intents = getattr(profile, "action_intents", None) or ()
    return tuple(str(getattr(item, "value", item)) for item in intents if str(item))


def _profile_complexity_hint(routed: object) -> str:
    """Router v2 决策 → 复杂度档位（``ATOMIC`` / ``SEQUENTIAL`` / ``DYNAMIC``）。"""
    if routed is None:
        return ""
    profile = getattr(routed, "profile", None)
    legacy = str(getattr(profile, "complexity", "") or "").upper()
    return {"M0": "ATOMIC", "M1": "ATOMIC", "M2": "SEQUENTIAL", "M3": "DYNAMIC"}.get(legacy, "")


def _legacy_shape_reasons(request: str) -> tuple[str, ...]:
    """旧词表理由（**只在**没有 Router v2 画像时作为兜底；不作为路由依据）。"""
    try:
        from app.agents.orchestration.task_shape import assess_task_shape

        return tuple(assess_task_shape(request).reasons)
    except Exception:  # noqa: BLE001 - 兜底不可用时留空（策略层按无副作用处理）
        return ()


class JobSubmissionService:
    """Execute an already-admitted submission without depending on the facade."""

    def __init__(
        self,
        *,
        store: JobRepository,
        context_service: SubmissionContextService,
        office_plan_selection: OfficePlanSelectionService,
        plan_compilation: PlanCompilationService,
        materialization: JobMaterializationService,
        temporal_static_mode: bool,
        temporal_logical_read_mode: bool,
        temporal_logical_effects_mode: bool,
        can_run_static_temporal: Callable[[Job], bool],
        probe_temporal: Callable[[], Awaitable[bool]],
        static_backend: Any,
        logical_read_backend: Any,
        logical_effects_backend: Any,
        legacy_backend: Any,
        start_heartbeat: Callable[[str, str], None],
        stop_heartbeat: Callable[[str], Awaitable[None]],
        plan_with_context: Callable[[PlanRequestContext], Awaitable[Any]],
        plan_contexts: dict[str, dict],
        llm_configs: dict[str, dict],
    ) -> None:
        self._store = store
        self._context_service = context_service
        self._office_plan_selection = office_plan_selection
        self._plan_compilation = plan_compilation
        self._materialization = materialization
        self._temporal_static_mode = temporal_static_mode
        self._temporal_logical_read_mode = temporal_logical_read_mode
        self._temporal_logical_effects_mode = temporal_logical_effects_mode
        self._can_run_static_temporal = can_run_static_temporal
        self._probe_temporal = probe_temporal
        self._static_backend = static_backend
        self._logical_read_backend = logical_read_backend
        self._logical_effects_backend = logical_effects_backend
        self._legacy_backend = legacy_backend
        self._start_heartbeat = start_heartbeat
        self._stop_heartbeat = stop_heartbeat
        self._plan_with_context = plan_with_context
        self._plan_contexts = plan_contexts
        self._llm_configs = llm_configs

    def _discard_pending(self, job_id: str) -> None:
        self._plan_contexts.pop(job_id, None)

    async def _blocked_by_model_capability(
        self,
        *,
        user_id: str,
        user_role: str,
        request: str,
        scene: str,
        conversation_id: str | None,
        submission_key: str,
        admission_token: str,
        routing: dict,
        model_routing: dict,
    ) -> Job:
        """能力不足的**硬阻断**：不调用任何模型，直接物化一个可解释终态 Job。

        路径与能力预检阻断一致（澄清型终态、不派发）；结构化结论与 process 载荷
        一并写进 ``routing``，前端据此说明"为什么没执行"。
        """
        from app.agents.orchestration.planning.contracts import TaskTree

        from app.core.model_capability_router import attach_model_routing

        message = str(model_routing.get("safe_message") or "").strip() or (
            "当前没有满足任务能力要求的模型，无法继续执行该任务。"
        )
        blocked_routing = attach_model_routing(
            {
                **routing,
                "planner_invoked": False,
                "tca_invoked": False,
                "fallback_action": "model_capability_blocked",
            },
            model_routing,
        )
        tree = TaskTree(nodes=[], clarification=message, plan_text="模型能力不足")
        materialized = await self._materialization.materialize(
            user_id=user_id,
            user_role=user_role,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            submission_key=submission_key,
            tree=tree,
            routing=blocked_routing,
        )
        job = materialized.job
        if not materialized.terminal:
            # 必须终态：能力不足的任务绝不派发执行（否则就是用错模型在跑）。
            job.status = JobStatus.FAILED
            job.error = message
            job.result = {
                "type": "model_capability_blocked",
                "error_code": str(model_routing.get("error_code") or "CAPABILITY_UNAVAILABLE"),
                "message": message,
                "retryable": False,
            }
        await self._store.create_job(job)
        await job_admission.release(token=admission_token)
        self._discard_pending(job.job_id)
        return job

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
        workspace_id: str | None,
        user_role: str,
        submission_key: str,
        admission_token: str,
        execution_preference: str = "use_workspace_policy",
        timeout_seconds: float | None = None,
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
            workspace_id=workspace_id,
        )
        office_docs = prepared.office_docs
        effective_llm = prepared.effective_llm
        llm_config = effective_llm.as_dict()
        routing_model = effective_llm.public_dict()
        planning_context = prepared.planning_context
        routing: dict = {"llm": routing_model} if scene == "office" else {}

        # ── 模型计划冻结（方案 §七）──
        # 任务创建时解析一次"角色 → 档位 → 模型"，之后计划/执行/重试/恢复都用这份
        # 计划，避免管理员中途改配置导致同一任务前后用不同模型。Job 快照里只放
        # 公开部分（角色/档位/模型/来源），**不含任何 API Key**。
        #
        # 灰度 ``MODEL_CAPABILITY_ROUTER_V2``：打开时按任务能力需求过滤候选/换档，
        # 结论冻结进计划；关闭时 build_model_plan 的调用参数与行为逐字不变。
        from app.core.feature_flags import feature_enabled

        model_router_enabled = feature_enabled("MODEL_CAPABILITY_ROUTER_V2")
        model_routing: dict | None = None
        try:
            from app.core.model_plan import build_model_plan

            plan_kwargs: dict = {}
            if model_router_enabled:
                plan_kwargs["requirements"] = _capability_requirements(
                    scene=scene, request=request, office_docs=office_docs
                )
            model_plan = await build_model_plan(
                scene=scene,
                user_id=user_id,
                byok_key=str(llm_api_key or "") if effective_llm.byok else None,
                **plan_kwargs,
            )
            routing["model_plan"] = model_plan.public_dict()
            if model_router_enabled:
                model_routing = dict(getattr(model_plan, "model_routing", {}) or {}) or None
        except Exception as exc:  # noqa: BLE001 - 计划冻结失败不能阻断任务提交
            logger.warning("[model-plan] 冻结失败（继续用现有配置链）: {}", str(exc)[:160])

        # 能力阻断（工具/模态缺失）：**不调用任何模型**，直接给出可解释终态。
        if model_routing is not None and model_routing.get("blocked"):
            return await self._blocked_by_model_capability(
                user_id=user_id,
                user_role=user_role,
                request=request,
                scene=scene,
                conversation_id=conversation_id,
                submission_key=submission_key,
                admission_token=admission_token,
                routing=routing,
                model_routing=model_routing,
            )

        if scene == "office":
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
        else:
            tree = await self._plan_with_context(planning_context)

        # 模型路由结论进快照（开关关闭时为 None，routing 逐字不变）。
        # 注意：office 场景的 routing 由计划选择器重建，因此必须在这里（重建之后）
        # 写入；结论只描述"选了哪个档位/为什么"，不改动既有路由字段。
        if model_routing is not None:
            from app.core.model_capability_router import attach_model_routing

            routing = attach_model_routing(routing, model_routing)
            if scene == "office":
                routing.setdefault("model_plan", model_plan.public_dict())

        self._plan_compilation.normalize_for_submission(
            tree.nodes,
            request,
            # Dependencies are declared by the LLM JobSpec and validated by
            # the compiler.  Do not collapse arbitrary plans into a serial
            # chain; that changes the task semantics and defeats node-level
            # concurrency.
            preserve_dependencies=True,
            complexity_level=str(routing.get("level") or ""),
        )
        if scene == "office" and tree.nodes and not tree.error:
            tree = await self._plan_compilation.compile_with_feedback(
                tree,
                routing=routing,
                user_role=user_role,
                context=planning_context,
            )
        if scene == "office":
            routing["workspace_id"] = str(workspace_id or "") or None
            # 执行授权快照写入 Job 元数据（含 user/conversation/device/policy）。
            workspace_grant = dict(getattr(planning_context, "workspace_grant", {}) or {})
            if workspace_grant:
                routing["workspace_grant"] = workspace_grant
            # 由工作区 approval_mode 推导 execution_mode（use_workspace_policy 语义），
            # 并记录本轮规范执行状态（计划优先时 step_confirm → waiting_run）。
            from lumi_orch.execution_mode import (
                initial_execution_state,
                plan_first_eligible,
                resolve_execution_mode,
            )

            from app.core.config import settings as _settings

            grant_mode = str(workspace_grant.get("approval_mode") or "") or ""
            # “存在授权快照”要求实际绑定工作区/设备：未绑定时 WorkspaceContext
            # 只带保守默认 approval_mode，不能据此自动启用计划优先。
            has_grant = bool(
                workspace_grant.get("workspace_id") or workspace_grant.get("device_id")
            )
            routing["execution_mode"] = resolve_execution_mode(
                preference=execution_preference, approval_mode=grant_mode,
            )
            # 计划优先启用判定：显式 step_confirm 恒启用；未显式指定时仅当
            # 全局开关开启 + 工作区 manual_commit 授权快照存在才启用。
            plan_first = plan_first_eligible(
                scene=scene,
                requires_orchestration=True,
                execution_preference=execution_preference,
                approval_mode=grant_mode,
                plan_first_global=bool(getattr(_settings, "EXECUTION_PLAN_FIRST", False)),
                has_workspace_grant=has_grant,
            )
            routing["execution_state"] = initial_execution_state(
                routing["execution_mode"],
                plan_first_enabled=plan_first,
            )
            # 计划步骤元数据持久化：供前端 plan/步骤恢复与未来单步执行使用；
            # plan_revision/current_step_index 由补丁或运行器后续推进。
            if "steps" not in routing:
                from lumi_orch.run_view import steps_from_nodes

                routing["steps"] = steps_from_nodes(tree.nodes or [])
            routing.setdefault("plan_revision", 1)
            routing.setdefault("current_step_index", 0)
            if prepared.pending_office_docs and not office_docs:
                names = "、".join(
                    str(item.get("filename") or "文档")[:120]
                    for item in prepared.pending_office_docs[:3]
                )
                tree = type(tree)(
                    nodes=[],
                    clarification=(
                        f"工作区文档正在解析：{names}。解析完成后我会基于文档内容回答，请稍后重新发送。"
                    ),
                    plan_text="等待工作区文档解析完成",
                )
                routing["fallback_action"] = "workspace_document_pending"
            routing["input_refs"] = [
                {
                    "doc_id": str(item.get("doc_id") or ""),
                    "filename": str(item.get("filename") or "")[:500],
                    "kind": str(item.get("kind") or "")[:20],
                }
                for item in office_docs or []
                if item.get("doc_id")
            ]
            # 项目代码访问是显式授权能力：仅复制 API 请求中提供的项目 ID，
            # 不从规划器推断的节点参数或用户文本回填授权范围。
            authorized_projects: list[str] = []
            for value in [project_id, *(project_ids or [])]:
                text = str(value or "").strip()
                if text and text not in authorized_projects:
                    authorized_projects.append(text)
            routing["authorized_project_ids"] = authorized_projects

        # 路由快照：**唯一**由 route_snapshot 投影器写入（Router v2 权威；
        # 旧执行策略只提供 compat 字段；两个开关都关时策略字段清空）。
        if scene == "office":
            from app.core.config import settings as _policy_settings

            from app.agents.orchestration.route_snapshot import (
                apply_route_snapshot,
                build_route_snapshot,
            )

            router_v2_enabled = bool(getattr(_policy_settings, "TASK_ROUTER_V2_ENABLED", False))
            policy_v2_enabled = bool(getattr(_policy_settings, "EXECUTION_POLICY_V2_ENABLED", False))
            # 先出 Router v2 决策，再让策略层**消费同一份画像**：方案 4 §1.2 要求
            # 入口只评估一次，策略层不得再用另一套词表解析同一段用户原文
            # （两处结论打架正是"用户要创建文件、模型只拿到读取工具"的根因）。
            router_meta = None
            routed = None
            if router_v2_enabled:
                from app.services.task_assessor import AssessmentContext
                from app.services.task_router_adapter import plan_and_route

                routed = await plan_and_route(
                    request=request,
                    context=AssessmentContext(
                        request=request,
                        has_attachments=False,
                        has_office_docs=bool(office_docs),
                        workspace_id=str(workspace_id or ""),
                        workspace_bound=bool(workspace_id),
                    ),
                    use_llm=False,  # 提交阶段已有 Planner，避免二次模型调用
                )
                router_meta = routed.meta()

            policy_meta = None
            if policy_v2_enabled:
                from lumi_orch.execution_policy import (
                    TaskEntrySignals,
                    policy_meta_from_signals,
                )

                signals = TaskEntrySignals(
                    request=request,
                    scene=scene,
                    reasons=tuple(_legacy_shape_reasons(request)),
                    has_attachments=False,
                    has_office_docs=bool(office_docs),
                    workspace_available=bool(workspace_id),
                    web_search_enabled=False,
                    conversation_has_workspace=bool(workspace_id),
                    # 画像事实优先（有 Router v2 决策时；否则留空走旧 reasons 兜底）。
                    action_intents=_profile_action_intents(routed),
                    complexity_hint=_profile_complexity_hint(routed),
                    needs_runtime_decision=(
                        bool(getattr(routed.profile, "has_runtime_decision", False)) if routed is not None else None
                    ),
                )
                policy_meta = policy_meta_from_signals(signals, enabled=True)

            apply_route_snapshot(
                routing,
                build_route_snapshot(
                    router_v2_enabled=router_v2_enabled,
                    execution_policy_v2_enabled=policy_v2_enabled,
                    router_meta=router_meta,
                    policy_meta=policy_meta,
                    existing=routing,
                ),
            )
            if router_meta is not None:
                # ExecutionRequest：链路 TaskProfile → RouteDecision → ExecutionRequest。
                # 身份只来自服务端上下文；快照里不重复存用户原文（只留长度+哈希）。
                from app.contracts import ServerContext
                from app.contracts.routing import execution_request_snapshot

                execution_request = routed.execution_request(
                    request,
                    context=ServerContext(
                        user_id=user_id,
                        user_role=user_role,
                        conversation_id=str(conversation_id or ""),
                        workspace_id=str(workspace_id or ""),
                    ),
                )
                routing["execution_request"] = execution_request_snapshot(execution_request)

        # 内部执行超时预算：按复杂度档位取阶梯值（M0=5/M1=10/M2=30/M3=60），
        # 模型计划规模可升级档位，请求里的 timeout_seconds 可覆盖。写进 routing
        # 作为可审计字段，供后续内部等待（MCP/工作区/节点）有界化时取用。
        from app.agents.orchestration.timeout_ladder import describe_budget

        routing["timeout"] = describe_budget(
            tier=routing.get("level"),
            node_count=len(getattr(tree, "nodes", None) or []),
            override=timeout_seconds,
        )
        # 插件/能力/策略快照：记录"这次任务当时用了哪个 Provider/设备/契约版本/策略包"，
        # 刷新与回滚后仍可解释（routing 只放审计摘要，不放过程日志与正文）。
        from app.agents.capabilities.policy_packs import select_policy_id
        from app.agents.capabilities.snapshots import attach_job_snapshots

        approval_mode = str((routing.get("workspace_grant") or {}).get("approval_mode") or "")
        policy_id = select_policy_id(
            approval_mode=approval_mode,
            risk_level=str((routing.get("task_profile") or {}).get("risk_level") or ""),
            side_effects=(routing.get("task_profile") or {}).get("side_effects") or [],
        )
        routing["policy_id"] = policy_id
        attach_job_snapshots(
            routing,
            approval_mode=approval_mode,
            execution_mode=str(routing.get("execution_mode") or ""),
            policy_id=policy_id,
        )
        # required_capabilities 解析：把 TaskProfile 的**抽象能力**翻成具体能力，并
        # 记录当前绑定下有没有 Provider（缺 Provider 的结论与"安装/启用"提示一并落
        # routing，供计划阶段提示与后续步骤级前置门禁使用）。
        # 注意：本阶段**不因缺能力拒收任务**——计划本身仍有价值，且客户端可能随后
        # 连接；硬性前置失败落在"即将产生副作用的那一步"（由 Broker 返回
        # CAPABILITY_MISSING，前端据此提示连接设备/安装 Provider）。
        try:
            from app.agents.capabilities.broker import capability_broker
            from app.agents.capabilities.resolver import (
                CapabilityResolver,
                concrete_capabilities,
            )
            from lumi_contracts.plugins import SessionBinding

            required = concrete_capabilities(
                list((routing.get("task_profile") or {}).get("required_capabilities") or [])
                or list((routing.get("route_decision") or {}).get("required_capabilities") or [])
            )
            if required:
                resolution = CapabilityResolver(broker=capability_broker).resolve(
                    required,
                    binding=SessionBinding(
                        user_id=user_id,
                        conversation_id=str(conversation_id or ""),
                        workspace_id=str(workspace_id or ""),
                    ),
                )
                routing["capability_resolution"] = attach_capability_resolution_time(
                    resolution.as_dict()
                )
                # 提交只是**时点快照**：客户端可能稍后才连上/注册。把"重算这次结论所需的
                # 输入"一并落库，读取路径才能刷新（见 ``refresh_capability_resolution``），
                # 否则前端会一直显示"缺少必需能力 / 没有可用的 Provider"，
                # 而同一份工作区其实读得好好的。
                routing["capability_resolution_query"] = {
                    "required": list(required),
                    "user_id": str(user_id or ""),
                    "conversation_id": str(conversation_id or ""),
                    "workspace_id": str(workspace_id or ""),
                }
        except Exception as exc:  # noqa: BLE001 - 解析失败不能阻断提交
            logger.warning("能力解析降级（忽略）: {}", str(exc)[:160])
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
                "workspace_id": workspace_id,
            }
            self._llm_configs[job.job_id] = llm_config

        if materialized.terminal:
            await self._store.create_job(job)
            await job_admission.release(token=admission_token)
            self._discard_pending(job.job_id)
            return job

        # 计划优先（step_confirm）：首轮只生成计划并置 waiting_run，不派发执行。
        # 由 /jobs/{id}/resume（action=run_next）逐步骤驱动。
        if (job.routing or {}).get("execution_state") == "waiting_run":
            # 计划已就绪但任务停放：不占用准入槽位、不开心跳；plan_text 与完整
            # 步骤节点保留在快照，供前端恢复计划与 run_next 逐步骤驱动。
            job.status = JobStatus.PENDING
            job.routing = {**(job.routing or {}), "plan_text": str(job.plan_text or "")[:12000]}
            await self._store.create_job(job)
            await job_admission.release(token=admission_token)
            self._discard_pending(job.job_id)
            return job

        await job_admission.promote(admission_token, job.job_id, user_id)
        self._start_heartbeat(job.job_id, user_id)
        # 工具注册表影子对比（P1）：一次任务一次打点，回答"派生有没有跟静态表漂移"。
        # 只记录、不改行为，且失败绝不影响提交。
        try:
            from app.agents.capabilities.tool_registry import log_shadow_differences

            log_shadow_differences(scene=scene, job_id=job.job_id)
        except Exception:  # noqa: BLE001
            pass
        # v2 观测：进入 Planner 编排的计数（指标默认关闭时零开销）。
        if scene == "office":
            from app.core.observability import inc_planner_invoked

            policy = (job.routing or {}).get("execution_policy")
            if policy:
                inc_planner_invoked(
                    str(policy), str((job.routing or {}).get("complexity") or "")
                )
        try:
            effects_decision = None
            if (
                self._temporal_logical_effects_mode
                and isinstance((job.routing or {}).get("logical_plan"), dict)
            ):
                from app.agents.orchestration.logical_plan import load_logical_plan
                from app.agents.orchestration.runtime_gateway import RuntimeGateway

                pointer = (job.routing or {}).get("logical_plan") or {}
                plan = await load_logical_plan(job.user_id, str(pointer.get("plan_id") or ""))
                effects_decision = RuntimeGateway.logical_effects_rollout_eligibility(job, plan)
                job.routing = {
                    **(job.routing or {}),
                    "temporal_logical_effects_eligibility": {
                        "eligible": effects_decision.eligible,
                        "code": effects_decision.code,
                        "detail": effects_decision.detail,
                    },
                }
            if (
                self._temporal_logical_effects_mode
                and effects_decision is not None
                and effects_decision.eligible
                and await self._probe_temporal()
            ):
                try:
                    await self._logical_effects_backend.submit(job, effective_llm.api_key, llm_config)
                    logger.info("审批副作用逻辑计划已提交(Temporal): {}", job.job_id[:8])
                    return job
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Temporal 审批副作用逻辑计划提交失败，回退自建 DAG: {} | {}", job.job_id[:8], exc)
                    job.routing = {
                        **(job.routing or {}),
                        "runtime": "legacy",
                        "temporal_submit_error": str(exc)[:200],
                    }
            logical_decision = None
            if (
                self._temporal_logical_read_mode
                and isinstance((job.routing or {}).get("logical_plan"), dict)
            ):
                from app.agents.orchestration.logical_plan import load_logical_plan
                from app.agents.orchestration.runtime_gateway import RuntimeGateway

                pointer = (job.routing or {}).get("logical_plan") or {}
                plan = await load_logical_plan(job.user_id, str(pointer.get("plan_id") or ""))
                logical_decision = RuntimeGateway.logical_read_rollout_eligibility(job, plan)
                job.routing = {
                    **(job.routing or {}),
                    "temporal_logical_read_eligibility": {
                        "eligible": logical_decision.eligible,
                        "code": logical_decision.code,
                        "detail": logical_decision.detail,
                    },
                }
            if (
                self._temporal_logical_read_mode
                and logical_decision is not None
                and logical_decision.eligible
                and await self._probe_temporal()
            ):
                try:
                    await self._logical_read_backend.submit(job, effective_llm.api_key, llm_config)
                    logger.info(
                        "纯读逻辑计划已提交(Temporal): {} | frontier={} request={}",
                        job.job_id[:8],
                        len(job.nodes),
                        request[:40],
                    )
                    return job
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Temporal 纯读逻辑计划提交失败，回退自建 DAG: {} | {}",
                        job.job_id[:8],
                        exc,
                    )
                    job.routing = {
                        **(job.routing or {}),
                        "runtime": "legacy",
                        "temporal_submit_error": str(exc)[:200],
                    }
            static_decision = None
            if self._temporal_static_mode:
                from app.agents.orchestration.runtime_gateway import RuntimeGateway

                static_decision = RuntimeGateway.static_rollout_eligibility(job)
                job.routing = {
                    **(job.routing or {}),
                    "temporal_static_eligibility": {
                        "eligible": static_decision.eligible,
                        "code": static_decision.code,
                        "detail": static_decision.detail,
                    },
                }
            if (
                self._temporal_static_mode
                and static_decision is not None
                and static_decision.eligible
                and await self._probe_temporal()
            ):
                try:
                    await self._static_backend.submit(job, effective_llm.api_key, llm_config)
                    logger.info(
                        "静态只读 DAG 已提交(Temporal): {} | agent={} request={}",
                        job.job_id[:8],
                        [node.agent for node in job.nodes],
                        request[:40],
                    )
                    return job
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Temporal 静态 DAG 提交失败，回退自建 DAG: {} | {}",
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
        except Exception as exc:
            # 提交边界负责释放准入资源，并把规划/物化/后端提交异常收敛为
            # 一个可查询的终态 Job；异常不再冒泡成对话接口 500。
            await job_admission.release(job_id=job.job_id, user_id=user_id)
            await self._stop_heartbeat(job.job_id)
            public = map_task_error(exc)
            job.status = getattr(type(job.status), "FAILED", "failed")
            job.error = public.message
            job.result = {
                "type": "execution_error",
                "status": "failed",
                "error_code": public.code,
                "message": public.message,
                "retryable": public.retryable,
            }
            job.routing = {**(job.routing or {}), "error_code": public.code}
            await self._store.save_job(job)
            return job
