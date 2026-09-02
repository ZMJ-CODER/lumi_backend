"""规划器对应用层交付的稳定契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lumi_orch import ExpansionSlot

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.planning.context import PlanRequestContext


class TaskTree:
    """规划结果：一组带依赖、可继续扩展的任务节点。"""

    def __init__(
        self,
        nodes: list[TaskNode],
        clarification: str | None = None,
        plan_text: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        expansion_slots: list[ExpansionSlot] | None = None,
    ) -> None:
        self.nodes = nodes
        self.clarification = clarification
        self.plan_text = plan_text
        self.error = error
        self.error_code = error_code
        self.expansion_slots = list(expansion_slots or [])


class PlannerModelError(RuntimeError):
    """规划模型不可用时可展示的错误；不得伪装为知识检索失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Planner(ABC):
    """规划器稳定接口，兼容历史 ``plan`` 插件签名。"""

    async def plan_context(self, context: PlanRequestContext) -> TaskTree:
        summary = context.prior_summaries
        context_lines: list[str] = []
        if context.recent_messages:
            context_lines.append("最近对话：" + " | ".join(context.recent_messages[-6:]))
        if context.recent_artifacts:
            context_lines.append(
                "最近产物："
                + ", ".join(
                    str(item.get("filename") or item.get("artifact_type") or "未命名产物")
                    for item in context.recent_artifacts[-6:]
                )
            )
        if context.previous_plan:
            context_lines.append("上一计划：" + str(context.previous_plan.get("plan_text") or "已存在上一计划"))
        if context_lines:
            summary = (summary + "\n" if summary else "") + "\n".join(context_lines)
        args = list(context.as_legacy_args())
        args[-1] = summary
        try:
            if context.llm_config is None:
                return await self.plan(*args)
            return await self.plan(*args, llm_config=context.llm_config)
        except TypeError as exc:
            if "llm_config" not in str(exc):
                raise
            return await self.plan(*args)

    @abstractmethod
    async def plan(
        self,
        user_id: str,
        request: str,
        scene: str = "office",
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
        prior_summaries: str = "",
        *,
        llm_config: dict | None = None,
    ) -> TaskTree: ...
