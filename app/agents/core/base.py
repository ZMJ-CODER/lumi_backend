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
    user_role: str = "user"
    # BYOK：agent 任务提交时临时携带的 API key（内存持有，任务结束即释放，不落库）
    llm_api_key: str | None = None
    # Original current-turn user request; only this may authorize a narrow
    # destructive-action confirmation bypass.
    user_request: str = ""
    # Optional text delta sink for office tasks.  The orchestrator wires this
    # to a short-lived Redis stream so API/SSE consumers can render text while
    # a node is still running.  File/document generators intentionally leave
    # it unset and return a reviewable artifact instead.
    on_output: object | None = None
    # Compatibility/audit field. Execution authorization is bound to exact
    # normalized tool arguments through ``confirmed_tool_calls`` below.
    confirmed_tools: frozenset[str] = frozenset()
    confirmed_tool_calls: frozenset[str] = frozenset()


class WorkerAgent(ABC):
    """执行层 agent 基类."""

    name: str = ""
    description: str = ""
    # 规划器提示词用的参数说明（新增 agent 时填写，规划器会自动展示并允许调度）
    params_help: str = ""
    # 兼容元数据：用于描述角色的常用技能，不再作为运行时白名单。
    # 能力边界统一由 scene / permission / confirmation / write_op 策略决定。
    skills: list[str] = []

    @abstractmethod
    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        """执行一个任务节点，返回结构化结果."""
        ...

    async def run_skill(self, skill_name: str, params: dict, ctx: WorkerContext) -> dict:
        """调用技能并统一包装结果（带审计，复用技能体系）."""
        skill = SkillRegistry.get(skill_name)
        if skill is None:
            return {"success": False, "error": f"技能不存在: {skill_name}", "error_code": "SKILL_NOT_FOUND"}
        if not skill.supports_scene(ctx.scene):
            return {
                "success": False,
                "error": f"技能 {skill_name} 不支持场景 {ctx.scene}",
                "error_code": "FORBIDDEN",
            }
        # 规则引擎：硬逻辑动态校验（必填字段/阈值/权限），不交给 LLM
        from app.agents.rules import check_rules

        violations = check_rules(self.name, skill_name, params, ctx.user_id, ctx)
        if violations:
            return {
                "success": False,
                "error": "规则校验未通过：" + "；".join(violations[:3]),
                "error_code": "RULE_VIOLATION",
            }
        from app.agents.skills.executor import execute_tool_call

        result = await execute_tool_call(
            {
                "id": f"worker-{self.name}-{skill_name}",
                "type": "function",
                "function": {"name": skill_name, "arguments": params},
            },
            ctx.user_id,
            ctx.scene,
            ctx.job_id,
            user_role=ctx.user_role,
            user_message=ctx.user_request,
            confirmed_tools=ctx.confirmed_tools,
            confirmed_tool_calls=ctx.confirmed_tool_calls,
            on_output=ctx.on_output,
        )
        if not result.success:
            return {
                "success": False,
                "error": result.error,
                "error_code": result.error_code,
                "retryable": result.retryable,
                "tool_metadata": result.metadata,
                "tool": skill_name,
                "approval_fingerprint": str(result.metadata.get("approval_fingerprint") or ""),
            }
        return {"success": True, "content": result.output, **result.metadata}

    def __repr__(self) -> str:
        return f"<WorkerAgent: {self.name} skills={self.skills}>"
