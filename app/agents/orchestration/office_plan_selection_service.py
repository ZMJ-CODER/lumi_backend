"""通过复杂度遥测和统一 LLM 入口选择办公工作流计划。"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

from lumi_contracts.events.process import ProcessLogEntry

from app.agents.orchestration.capability_preflight import PreflightStatus
from app.agents.orchestration.capability_preflight_service import (
    PREFLIGHT_NOTICE_ENTRY_ID,
    PREFLIGHT_NOTICE_KEY,
    CapabilityPreflightService,
    attach_preflight,
    preflight_process_notice,
)
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import TaskTree
from app.agents.orchestration.task_preflight import (
    capability_preflight_facts,
    clarification_for_request,
    preflight_external_effect,
)
from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor
from app.agents.orchestration import office_plan_strategies as strategies

#: canonical 画像生产开关（与 ``app.services.task_assessor.CANONICAL_FLAG`` 同名，避免循环导入）。
CANONICAL_FLAG = "TASK_PROFILE_CANONICAL"


@dataclass(slots=True)
class OfficePlanSelection:
    tree: Any
    routing: dict
    level: ComplexityLevel
    #: 预检失败的 process 事件载荷（canonical 画像接入后才有；见 ``preflight_process_frame``）。
    process_notice: dict | None = None


def _broker_capability_probe(planning_context: PlanRequestContext):
    """把 Broker 的**事实**探测包成 ``BrokerProbe``（只回原因词，不判定）。

    探测自身故障一律视为"没有事实"（返回空），不得让预检机制故障阻断既有链路。

    阶段 4（灰度 ``PLUGIN_QUOTA_ENFORCEMENT``）：被停用/卸载的插件所提供的能力，
    在这里补一条 ``capability_unavailable`` 事实——**复用既有预检链路**得到
    ``CAPABILITY_UNAVAILABLE``，不新增阻断机制。开关关闭时
    ``plugin_blocked_capabilities()`` 恒为空集合，且不会构造插件管理器。
    """

    def probe(capabilities: Any) -> dict[str, str]:
        try:
            from lumi_contracts.plugins import SessionBinding

            from app.agents.capabilities.broker import capability_broker

            binding = SessionBinding(
                user_id=str(planning_context.user_id or ""),
                workspace_id=str(planning_context.workspace_id or ""),
            )
            facts = dict(capability_broker.preflight_facts(capabilities, binding=binding))
            blocked = _plugin_blocked_capabilities()
            if blocked:
                for item in capabilities or ():
                    name = str(item or "").strip()
                    if name and name.split("@", 1)[0] in blocked and name not in facts:
                        facts[name] = "capability_unavailable"
            return facts
        except Exception as exc:  # noqa: BLE001 - 探测失败不构成预检结论
            logger.debug("能力预检探测降级（按无事实处理）: {}", str(exc)[:160])
            return {}

    return probe


def _plugin_blocked_capabilities() -> set[str]:
    """插件侧预检事实（开关关闭/管理器不可用时为空集合，绝不抛错）。"""
    try:
        from app.services.plugins.manager import plugin_blocked_capabilities

        return plugin_blocked_capabilities()
    except Exception as exc:  # noqa: BLE001 - 插件事实缺失不等于能力不可用
        logger.debug("插件预检事实降级（按无事实处理）: {}", str(exc)[:160])
        return set()


def _canonical_preflight_facts(
    *,
    request: str,
    planning_context: PlanRequestContext,
    project_id: str | None,
    office_docs: list[dict] | None,
) -> dict[str, Any]:
    """canonical TaskProfile → 预检事实（``TASK_PROFILE_CANONICAL`` 打开时用）。

    与旧 ``capability_preflight_facts`` 的差别**只在画像来源**：动作意图/抽象能力/
    目标范围来自 ``task_assessor`` 的 canonical 画像（同一 producer，确定性启发式分支，
    计划前不额外发起模型调用）；工作区绑定与"是否需要工作区"仍来自既有的服务端事实
    判定（``has_trusted_context`` / ``requires_local_scope``），本函数不新增事实。

    画像能力是**抽象**词表（契约要求，如 ``DOCUMENT_EDIT``），而预检探测的寻址键是
    具体能力名，因此这里用 ``concrete_capabilities`` 做唯一翻译；翻译不出具体能力的
    抽象能力（如服务端已有等价实现的 ``WEB_RESEARCH``）**留空**（不猜、不阻断），
    抽象原名进 ``required_capabilities_abstract`` 供审计。
    """
    from app.agents.capabilities.resolver import concrete_capabilities
    from app.core.feature_flags import shadow_mode
    from app.services.task_assessor import AssessmentContext, canonical_profile

    facts = capability_preflight_facts(
        request,
        workspace_id=planning_context.workspace_id,
        project_id=project_id,
        office_docs=office_docs,
    )
    workspace_id = str(planning_context.workspace_id or project_id or "").strip()
    profile = canonical_profile(
        AssessmentContext(
            request=request,
            has_attachments=bool(office_docs),
            has_office_docs=bool(office_docs),
            workspace_id=workspace_id,
            workspace_bound=bool(str(planning_context.workspace_id or "").strip()),
        )
    )
    if shadow_mode():
        # 影子模式：canonical 只用于记录 old→new 差异（``canonical_profile`` 内部已
        # 打一行 fingerprint 日志），**实际事实来源仍是旧判定**——即开关关闭时的形状。
        return {
            **facts["profile"],
            "workspace_bound": bool(facts["workspace_bound"]),
            "requires_workspace": bool(facts["requires_workspace"]),
        }
    abstract = [str(item) for item in profile.required_capabilities]
    return {
        "intent_type": str(profile.intent_type),
        "action_intents": [str(item) for item in profile.action_intents],
        "target_scope": str(profile.target_scope),
        "target_clarity": str(profile.target_clarity),
        # 预检探测用具体能力名；抽象能力留档，便于排障与影子比对。
        "required_capabilities": concrete_capabilities(abstract),
        "required_capabilities_abstract": abstract,
        "approval_required": bool(profile.approval_required),
        "workspace_bound": bool(facts["workspace_bound"]),
        "requires_workspace": bool(facts["requires_workspace"]),
    }


def preflight_process_frame(routing: Any) -> dict[str, Any] | None:
    """routing 里的预检失败载荷 → SSE ``process`` 帧（没有则返回 None）。

    只加这一帧；既有 job/step/delta/done 帧的形状与顺序不变。``entry_id`` 与过程
    日志投影（``app/contracts/process_log.py``）一致，刷新后合并成同一行。
    """
    notice = routing.get(PREFLIGHT_NOTICE_KEY) if isinstance(routing, Mapping) else None
    if not isinstance(notice, Mapping) or not notice:
        return None
    entry = ProcessLogEntry.from_event(
        {"entry_id": PREFLIGHT_NOTICE_ENTRY_ID, **dict(notice)}
    )
    return {"type": "process", "content": entry.summary, **entry.to_sse_fields()}


class OfficePlanSelectionService:
    """Keep normal-office plan selection separate from Job lifecycle writes."""

    def __init__(
        self,
        *,
        planner: Any,
        workers: dict,
        assessor: TaskComplexityAssessor,
        preflight_service: CapabilityPreflightService | None = None,
        preflight_profile: Any = None,
        preflight_probe: Any = None,
        preflight_registered_tools: Any = None,
    ) -> None:
        self._planner = planner
        self._workers = workers
        self._assessor = assessor
        # 预检唯一入口；``enabled()`` 为假时本服务完全不介入（旧路径逐字保留）。
        self._preflight = preflight_service or CapabilityPreflightService()
        # 画像/探测/已注册工具的事实来源可注入（测试与未来 canonical TaskProfile）。
        self._preflight_profile = preflight_profile
        self._preflight_probe = preflight_probe
        self._preflight_registered_tools = preflight_registered_tools

    # 预规划策略（文档快路径 / 工作区快路径 / 覆盖兜底 / 补偿注入）已移到
    # ``office_plan_strategies``：service 只保留编排顺序与路由记录。

    def _preflight_inputs(
        self,
        *,
        request: str,
        project_id: str | None,
        office_docs: list[dict] | None,
        planning_context: PlanRequestContext,
    ) -> dict[str, Any]:
        """预检输入：只放服务端已知事实，不猜动作/能力（避免误拦合法任务）。

        注册的画像来源可以返回 ``workspace_bound`` / ``requires_workspace`` 覆盖键
        （canonical TaskProfile 接入后由画像给出这两个事实）。

        ``TASK_PROFILE_CANONICAL`` 打开时，画像改用 canonical TaskProfile 生产
        （见 :func:`_canonical_preflight_facts`）；关闭时逐字走旧事实判定
        ``capability_preflight_facts``（默认关 = 今天的行为）。
        """
        from app.core.feature_flags import feature_enabled

        if callable(self._preflight_profile):
            supplied = dict(
                self._preflight_profile(
                    request=request,
                    planning_context=planning_context,
                    project_id=project_id,
                )
                or {}
            )
        elif feature_enabled(CANONICAL_FLAG):
            supplied = _canonical_preflight_facts(
                request=request,
                planning_context=planning_context,
                project_id=project_id,
                office_docs=office_docs,
            )
        else:
            facts = capability_preflight_facts(
                request,
                workspace_id=planning_context.workspace_id,
                project_id=project_id,
                office_docs=office_docs,
            )
            supplied = dict(facts["profile"])
            supplied["workspace_bound"] = bool(facts["workspace_bound"])
            supplied["requires_workspace"] = bool(facts["requires_workspace"])
        workspace_bound = supplied.pop("workspace_bound", None)
        requires_workspace = supplied.pop("requires_workspace", None)
        # 画像未给出能力时（开关关闭 / 画像来源没有能力事实）：不注入能力、不因
        # "工具未注册"阻断——缺能力不得在计划前拒收任务（既有契约）。
        supplied.setdefault("required_capabilities", [])
        return {
            "profile": supplied,
            "workspace_bound": (
                bool(workspace_bound)
                if workspace_bound is not None
                else bool(str(planning_context.workspace_id or project_id or "").strip())
            ),
            "requires_workspace": bool(requires_workspace),
        }

    def _blocked_selection(
        self, result: Any, *, started: float, request: str, planning_context: PlanRequestContext,
        project_id: str | None, office_docs: list[dict] | None,
    ) -> OfficePlanSelection:
        """预检失败：不调用主模型、不给空工具窗口，结论并进 routing。"""
        wording = clarification_for_request(
            request,
            workspace_id=planning_context.workspace_id,
            project_id=project_id,
            office_docs=office_docs,
        )
        answer = (
            wording.question
            or result.question
            or (result.error.safe_message if result.error else "")
            or "当前任务无法继续执行。"
        )
        clarification = result.status == PreflightStatus.NEEDS_CLARIFICATION.value
        routing = attach_preflight(
            {
                "planner_invoked": False,
                "tca_invoked": False,
                "fallback_action": (
                    "preflight_clarification" if clarification else "preflight_blocked"
                ),
                "preflight_reason": wording.reason if clarification else result.error_code,
                "level": ComplexityLevel.M3.value,
                "route_latency_ms": int((time.perf_counter() - started) * 1000),
            },
            result,
        )
        # 预检失败的**过程事件**（客户端可见通道）：只有 canonical 画像接入（开关打开）
        # 时才有真实事实可报，因此只在该开关下产出；开关关闭时 routing 逐字不变。
        from app.core.feature_flags import feature_enabled

        notice = preflight_process_notice(result) if feature_enabled(CANONICAL_FLAG) else None
        if notice:
            routing[PREFLIGHT_NOTICE_KEY] = dict(notice)
        return OfficePlanSelection(
            tree=TaskTree(nodes=[], clarification=answer, plan_text="能力预检"),
            routing=routing,
            level=ComplexityLevel.M3,
            process_notice=notice,
        )

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
        # 灰度：``CAPABILITY_PREFLIGHT_V2`` 关闭时下面这段整体不执行，行为与改造前一致。
        if self._preflight.enabled():
            result = None
            try:
                inputs = self._preflight_inputs(
                    request=request,
                    project_id=project_id,
                    office_docs=office_docs,
                    planning_context=planning_context,
                )
                probe = self._preflight_probe or _broker_capability_probe(planning_context)
                result = self._preflight.preflight(
                    profile=inputs.get("profile"),
                    probe=probe if callable(probe) else None,
                    workspace_bound=bool(
                        inputs.get(
                            "workspace_bound",
                            bool(str(planning_context.workspace_id or project_id or "").strip()),
                        )
                    ),
                    requires_workspace=bool(inputs.get("requires_workspace", False)),
                    permissions=(
                        {str(item): True for item in (planning_context.permissions or ())} or None
                    ),
                    registered_tools=self._preflight_registered_tools,
                )
            except Exception as exc:  # noqa: BLE001 - 预检自身故障不得阻断既有链路
                logger.warning("能力预检降级（按旧路径继续）: {}", str(exc)[:160])
                result = None
            if result is not None and not result.ok:
                # APPROVAL_REQUIRED 走既有审批流程（不新建机制，也不在这里阻断）。
                if result.status != PreflightStatus.APPROVAL_REQUIRED.value:
                    return self._blocked_selection(
                        result,
                        started=started,
                        request=request,
                        planning_context=planning_context,
                        project_id=project_id,
                        office_docs=office_docs,
                    )
            selection = await self._select_plan(
                user_id=user_id,
                request=request,
                user_role=user_role,
                project_id=project_id,
                project_ids=project_ids,
                clarification_answer=clarification_answer,
                office_docs=office_docs,
                prior_summaries=prior_summaries,
                planning_context=planning_context,
                routing_model=routing_model,
                started=started,
                # 预检自身降级（没有结论）时保留旧澄清门，语义不丢。
                legacy_preflight=result is None,
            )
            if result is not None:
                # 唯一写入点：结论并入 routing（Job 快照 / preview 接口据此对前端可见）。
                selection.routing = attach_preflight(selection.routing, result)
            return selection

        return await self._select_plan(
            user_id=user_id,
            request=request,
            user_role=user_role,
            project_id=project_id,
            project_ids=project_ids,
            clarification_answer=clarification_answer,
            office_docs=office_docs,
            prior_summaries=prior_summaries,
            planning_context=planning_context,
            routing_model=routing_model,
            started=started,
            legacy_preflight=True,
        )

    async def _select_plan(
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
        started: float,
        legacy_preflight: bool,
    ) -> OfficePlanSelection:
        if legacy_preflight:
            # Run the cheap, domain-neutral side-effect guard before TCA.  Missing
            # target/scope must not spend even a classifier round trip.
            preflight = preflight_external_effect(
                request,
                workspace_id=planning_context.workspace_id,
                project_id=project_id,
                office_docs=office_docs,
            )
            if preflight.needs_clarification:
                return OfficePlanSelection(
                    tree=TaskTree(nodes=[], clarification=preflight.question, plan_text="执行前信息检查"),
                    routing={
                        "planner_invoked": False,
                        "tca_invoked": False,
                        "fallback_action": "preflight_clarification",
                        "preflight_reason": preflight.reason,
                        "level": ComplexityLevel.M3.value,
                        "route_latency_ms": int((time.perf_counter() - started) * 1000),
                    },
                    level=ComplexityLevel.M3,
                )
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
        # The public office entry bypasses this legacy compatibility path for
        # read-only context questions.  Keep it for explicit callers and
        # already-created jobs so older persisted plans can still be resumed;
        # it is not used to decide the normal request route anymore.
        fast_document_tree = strategies.document_read_path(request, list(office_docs or []), level)
        if fast_document_tree is not None:
            routing.update({
                "level": ComplexityLevel.M1.value,
                "planner_invoked": False,
                "fallback_action": "document_m1_legacy",
                "preserve_dependencies": True,
            })
            duration = time.perf_counter() - started
            routing["route_latency_ms"] = int(duration * 1000)
            return OfficePlanSelection(tree=fast_document_tree, routing=routing, level=ComplexityLevel.M1)
        # 工作区 + 单文件目标明确：先真的执行一次 workspace_navigator(read)，再把读到
        # 的事实交给文本节点回答。必须在 M0 直答与 Planner 之前判定，否则这类请求会
        # 落到无工具的 direct_llm 上，模型只能吐内部路由标记。
        workspace_tree = strategies.workspace_read_path(
            request,
            str(planning_context.workspace_id or ""),
            planning_context.workspace_summary,
        )
        if workspace_tree is not None:
            routing.update({
                "level": ComplexityLevel.M1.value,
                "planner_invoked": False,
                "fallback_action": "workspace_m1_read",
                "preserve_dependencies": True,
            })
            duration = time.perf_counter() - started
            routing["route_latency_ms"] = int(duration * 1000)
            return OfficePlanSelection(tree=workspace_tree, routing=routing, level=ComplexityLevel.M1)
        # 工作区 + 目标明确但文件未知 / 全目录处理：能力兜底（不依赖 Planner 想起
        # 聚合入口）。没有这条兜底，历史行为就是"生成了任务却没有注入工作区工具"，
        # 最终让无工具的文本节点输出内部路由标记。
        coverage_tree = strategies.workspace_coverage_path(
            request,
            str(planning_context.workspace_id or ""),
            planning_context.workspace_summary,
        )
        if coverage_tree is not None:
            routing.update({
                "level": ComplexityLevel.M1.value,
                "planner_invoked": False,
                "fallback_action": "workspace_coverage",
                "preserve_dependencies": True,
                "workspace_required": True,
            })
            duration = time.perf_counter() - started
            routing["route_latency_ms"] = int(duration * 1000)
            return OfficePlanSelection(
                tree=coverage_tree, routing=routing, level=ComplexityLevel.M1
            )
        if level == ComplexityLevel.M0:
            routing.update({
                "planner_invoked": False,
                "fallback_action": "m0_direct",
                "preserve_dependencies": True,
            })
            duration = time.perf_counter() - started
            routing["route_latency_ms"] = int(duration * 1000)
            return OfficePlanSelection(tree=strategies.direct_answer_path(request), routing=routing, level=level)
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
        # A syntactically failed empty planner result must not become the
        # user-visible answer for a read-only request.  Prefer the trusted
        # document path; otherwise return a clear direct answer bounded to the
        # user message. Provider/auth failures remain explicit errors.
        if str(getattr(tree, "error_code", "") or "").upper() == "PLANNER_EMPTY":
            fallback = strategies.document_read_path(request, list(office_docs or []), ComplexityLevel.M1)
            if fallback is None:
                fallback = strategies.direct_answer_path(request)
                routing["fallback_action"] = "planning_empty_direct"
            else:
                # Compatibility label retained for persisted telemetry; this
                # branch is only for an already-complex job, while normal
                # document Q&A bypasses the selector entirely.
                routing["fallback_action"] = "planning_empty_document_fallback"
                routing["level"] = ComplexityLevel.M1.value
            routing["planner_empty"] = True
            routing["planner_invoked"] = True
            tree = fallback
        else:
            routing["planner_invoked"] = True
        # 结构化补偿：任务画像/绑定表明"需要工作区"，但生成的计划里没有任何工作区
        # 读取节点时，不能就这么执行——那会让无工具的文本节点被迫吐内部路由标记。
        # 这里按能力（而不是关键词打补丁）补一个受控的发现步骤作为所有入度节点的新
        # 前置，原计划的步骤与依赖关系保持不变。
        if strategies.needs_workspace_discovery(tree, request, planning_context):
            tree = strategies.inject_workspace_discovery(tree, request, planning_context)
            routing["workspace_required"] = True
            routing["workspace_compensation"] = "WORKSPACE_REQUIRED"
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
