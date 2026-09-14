"""Capability-first office planner.

Office planning is authored by the LLM as abstract capability steps. This
module intentionally contains no keyword route classifier, business template,
concrete tool selection.
"""

from __future__ import annotations

import time

from loguru import logger

from app.agents.orchestration.office.document_scope import select_named_office_documents
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import Planner, PlannerModelError, TaskTree
from app.agents.orchestration.planning.prompting import build_planner_prompt, runtime_capability_note
from app.agents.orchestration.planning.task_profiles import AbstractTaskNode, TaskProfile, compile_abstract_tasks
from app.agents.skills.recovery import classify_model_error, is_terminal_model_error_code
from app.repositories.project_repository import ProjectRepository, SqlAlchemyProjectRepository


class LlmPlanner(Planner):
    """Translate a user goal into abstract, capability-based task nodes."""

    supports_context_planning = True

    def __init__(self, fallback: Planner | None = None, project_repository: ProjectRepository | None = None):
        self._project_repository = project_repository or SqlAlchemyProjectRepository()

    async def plan_for_level(self, level, user_id: str | None = None, request: str | None = None,
                             scene: str = "office", project_id: str | None = None,
                             project_ids: list[str] | None = None, llm_api_key: str | None = None,
                             clarification_answer: str | None = None, office_docs: list[dict] | None = None,
                             prior_summaries: str = "", bypass_fast_paths: bool = False, *,
                             context: PlanRequestContext | None = None) -> TaskTree:
        if context is None:
            context = PlanRequestContext.from_legacy_args(
                user_id or "", request or "", scene, project_id, project_ids,
                llm_api_key=llm_api_key, clarification_answer=clarification_answer,
                office_docs=office_docs, prior_summaries=prior_summaries,
            )
        return await self._plan_context(context, level)

    async def plan(self, user_id: str, request: str, scene: str = "office",
                   project_id: str | None = None, project_ids: list[str] | None = None,
                   llm_api_key: str | None = None, clarification_answer: str | None = None,
                   office_docs: list[dict] | None = None, prior_summaries: str = "",
                   *, llm_config: dict | None = None) -> TaskTree:
        context = PlanRequestContext.from_legacy_args(
            user_id, request, scene, project_id, project_ids,
            llm_api_key=llm_api_key, clarification_answer=clarification_answer,
            office_docs=office_docs, prior_summaries=prior_summaries, llm_config=llm_config,
        )
        return await self._plan_context(context, "m2")

    async def _plan_context(self, context: PlanRequestContext, level: object) -> TaskTree:
        docs = [dict(item) for item in context.office_docs]
        selected, unresolved, named = select_named_office_documents(context.request, docs)
        if named and unresolved:
            return TaskTree(nodes=[], clarification=f"未能唯一定位文件《{'、'.join(unresolved)}》。请确认准确名称后再试。")
        if named:
            docs = selected
        projects = await self._list_projects(context.user_id)
        try:
            tree = await self._plan_with_llm(
                context.user_id, context.request, context.project_id, list(context.project_ids),
                context.llm_api_key, projects, context.clarification_answer, docs,
                context.prior_summaries, llm_config=context.llm_config,
                workspace_id=context.workspace_id,
                workspace_summary=context.workspace_summary,
            )
        except PlannerModelError as exc:
            return TaskTree(nodes=[], error=str(exc), error_code=exc.code)
        if tree is None or (not tree.nodes and not tree.clarification):
            return TaskTree(nodes=[], error="任务规划未生成能力步骤，请稍后重试或补充目标。", error_code="PLANNER_EMPTY")
        level_value = str(getattr(level, "value", level)).lower()
        for node in tree.nodes:
            node.metadata = {**(node.metadata or {}), "complexity_level": level_value, "planning_complexity": level_value}
        return tree

    async def _list_projects(self, user_id: str) -> list[dict]:
        try:
            values = await self._project_repository.list_projects(user_id)
        except Exception:  # noqa: BLE001
            return []
        return [
            ({"id": str(v.get("id") or ""), "name": str(v.get("name") or "")} if isinstance(v, dict)
             else {"id": str(getattr(v, "id", "") or ""), "name": str(getattr(v, "name", "") or "")})
            for v in values
        ]

    async def _plan_with_llm(self, user_id: str, request: str, project_id: str | None,
                             project_ids: list[str] | None, llm_api_key: str | None,
                             projects: list[dict], clarification_answer: str | None = None,
                             office_docs: list[dict] | None = None, prior_summaries: str = "",
                             *, llm_config: dict | None = None, workspace_id: str | None = None,
                             workspace_summary: str = "") -> TaskTree | None:
        selected_ids = set((project_ids or []) + ([project_id] if project_id else []))
        visible_projects = [p for p in projects if p.get("id") in selected_ids]
        context = f"用户请求：{request}"
        if clarification_answer:
            context += f"\n用户补充说明：{clarification_answer}"
        if prior_summaries:
            context += f"\n此前任务摘要：{prior_summaries}"
        if visible_projects:
            context += f"\n已授权项目：{visible_projects}"
        if office_docs:
            context += "\n已授权附件：" + "、".join(str(d.get("filename") or "") for d in office_docs if d.get("doc_id"))
        if workspace_summary:
            # Planner 只消费目录/状态摘要，不消费文件正文或深层真实路径。
            context += "\n工作区摘要（只读）：\n" + str(workspace_summary).strip()
        elif workspace_id:
            context += "\n当前存在一个已授权的本地工作区；涉及读取、修改或运行项目时，将来源标为 SYSTEM_STATE，并在执行阶段由系统绑定工作区。"
        try:
            data = await self._call_structured_planner(
                user_id, request, context, llm_api_key, llm_config=llm_config, office_docs=office_docs,
            )
        except TypeError as exc:
            # Embedded deployments and tests may still override the stable
            # four-argument planner hook. This is call compatibility only;
            # its response remains subject to the abstract-task contract.
            if "llm_config" not in str(exc) and "office_docs" not in str(exc):
                raise
            data = await self._call_structured_planner(user_id, request, context, llm_api_key)
        if not data:
            return None
        clarification = str(data.get("clarification") or "").strip()
        raw = data.get("abstract_tasks") or []
        tasks: list[AbstractTaskNode] = []
        seen: set[str] = set()
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                node = AbstractTaskNode.model_validate(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("忽略无效抽象任务节点: {}", exc)
                continue
            if node.id not in seen:
                seen.add(node.id)
                tasks.append(node)
        if not tasks and isinstance(data.get("task_profile"), dict) and not clarification:
            try:
                tasks = [AbstractTaskNode(id="n1", name="完成用户目标",
                    profile=TaskProfile.model_validate(data["task_profile"]), instruction=request)]
            except Exception as exc:  # noqa: BLE001
                logger.debug("忽略无效任务画像: {}", exc)
        if clarification:
            return TaskTree(nodes=[], clarification=clarification)
        valid = {task.id for task in tasks}
        tasks = [task for task in tasks if all(dep in valid for dep in task.depends_on)]
        if not tasks:
            return None
        # Interaction policy is deliberately evaluated only after the LLM has
        # produced an abstract capability profile.  It never inspects the
        # user's business wording and therefore cannot become a keyword route.
        from app.agents.orchestration.planning.strategy_engine import strategy_engine

        snapshot = await strategy_engine.snapshot()
        has_prior_context = bool(clarification_answer or prior_summaries)
        for task in tasks:
            should_clarify, policy_id = strategy_engine.should_clarify(
                profile=task.profile,
                has_prior_context=has_prior_context,
                snapshot=snapshot,
            )
            if should_clarify:
                return TaskTree(
                    nodes=[],
                    clarification=(
                        "当前操作风险较高且目标信息不足。请确认要操作的对象、期望结果和授权范围后继续。"
                    ),
                    plan_text=f"交互策略 {policy_id} 要求先澄清",
                )
        nodes = await compile_abstract_tasks(
            tasks,
            user_id=user_id,
            user_request=request,
            scene="office",
            office_docs=office_docs,
            strategy_snapshot=snapshot,
        )
        return TaskTree(nodes=nodes, plan_text=str(data.get("plan") or "").strip() or None)

    async def _call_structured_planner(self, user_id: str, request: str, context: str,
                                       llm_api_key: str | None, *, llm_config: dict | None = None,
                                       office_docs: list[dict] | None = None) -> dict | None:
        started = time.perf_counter()
        try:
            from app.agents.langchain.planning import invoke_structured_planner
            prompt = build_planner_prompt() + await runtime_capability_note(request, user_id, office_docs) + "\n" + context
            output = await invoke_structured_planner(prompt, user_id=user_id, api_key=llm_api_key, llm_config=llm_config)
            return output.model_dump()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Planner] 抽象能力规划不可用: {}", exc)
            code, message = classify_model_error(exc)
            if is_terminal_model_error_code(code):
                raise PlannerModelError(code, message) from exc
            return None
        finally:
            logger.info("办公能力规划耗时: duration_ms={}", int((time.perf_counter() - started) * 1000))


__all__ = ["LlmPlanner", "Planner", "PlannerModelError", "TaskTree"]
