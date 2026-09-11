"""通过复杂度遥测和统一 LLM 入口选择办公工作流计划。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import TaskTree
from app.agents.orchestration.tca import ComplexityLevel, TaskComplexityAssessor
from app.agents.orchestration import office_plan_strategies as strategies


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

    # 预规划策略（文档快路径 / 工作区快路径 / 覆盖兜底 / 补偿注入）已移到
    # ``office_plan_strategies``：service 只保留编排顺序与路由记录。

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
        # Run the cheap, domain-neutral side-effect guard before TCA.  Missing
        # target/scope must not spend even a classifier round trip.
        from app.agents.orchestration.task_preflight import preflight_external_effect

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
