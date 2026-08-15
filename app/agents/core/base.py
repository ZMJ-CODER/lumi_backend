"""执行层 WorkerAgent 抽象 —— 集中定义 agent 基类与执行上下文.

一个 WorkerAgent = 一个专业角色 + 一组可调用的技能（复用技能插件体系）。
新增执行 agent 时继承 WorkerAgent，在 roles/<领域>/ 下实现并注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agents.skills.base import SkillContext
from app.agents.skills.registry import SkillRegistry

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


@dataclass
class WorkerContext:
    """Worker 执行上下文（编排器注入）."""

    user_id: str
    job_id: str
    scene: str = "office"
    # BYOK：agent 任务提交时临时携带的 API key（内存持有，任务结束即释放，不落库）
    llm_api_key: str | None = None


class WorkerAgent(ABC):
    """执行层 agent 基类."""

    name: str = ""
    description: str = ""
    # 规划器提示词用的参数说明（新增 agent 时填写，规划器会自动展示并允许调度）
    params_help: str = ""
    skills: list[str] = []  # 该 agent 可调用的技能名（白名单）

    @abstractmethod
    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        """执行一个任务节点，返回结构化结果."""
        ...

    async def run_skill(self, skill_name: str, params: dict, ctx: WorkerContext) -> dict:
        """调用技能并统一包装结果（带审计，复用技能体系）."""
        skill = SkillRegistry.get(skill_name)
        if skill is None:
            return {"success": False, "error": f"技能不存在: {skill_name}", "error_code": "SKILL_NOT_FOUND"}
        if self.skills and skill_name not in self.skills:
            return {"success": False, "error": f"agent '{self.name}' 无权调用技能 {skill_name}", "error_code": "FORBIDDEN"}
        # 规则引擎：硬逻辑动态校验（必填字段/阈值/权限），不交给 LLM
        from app.agents.rules import check_rules

        violations = check_rules(self.name, skill_name, params, ctx.user_id, ctx)
        if violations:
            return {
                "success": False,
                "error": "规则校验未通过：" + "；".join(violations[:3]),
                "error_code": "RULE_VIOLATION",
            }
        result = await skill.execute(
            params,
            SkillContext(
                user_id=ctx.user_id,
                scene=ctx.scene,
                conversation_id=ctx.job_id,
                llm_api_key=ctx.llm_api_key,
            ),
        )
        if not result.success:
            return {"success": False, "error": result.error, "error_code": result.error_code}
        return {"success": True, "content": result.output, **result.metadata}

    def __repr__(self) -> str:
        return f"<WorkerAgent: {self.name} skills={self.skills}>"
