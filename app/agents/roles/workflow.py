"""通用 Workflow Skill 执行角色。"""

from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as report_progress

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class WorkflowSkillAgent(WorkerAgent):
    """执行已通过计划编译器授权的公共或当前用户私有 Workflow Skill。"""

    name = "workflow_skill"
    description = "执行已注册的组合工作流 Skill；仅能使用其声明的受控 Tool"
    params_help = 'params 用 {"skill_name":"工作流名", "inputs":{}}'

    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        skill_name = str((node.params or {}).get("skill_name") or "").strip()
        inputs = (node.params or {}).get("inputs") or {}
        if not skill_name:
            return {"success": False, "error": "工作流节点缺少 skill_name", "error_code": "INVALID_ARGS"}
        if not isinstance(inputs, dict):
            return {"success": False, "error": "工作流节点 inputs 必须是对象", "error_code": "INVALID_ARGS"}
        await report_progress(ctx.job_id, node.id, "正在执行工作流…")
        result = await self.run_skill(skill_name, inputs, ctx)
        if result.get("success"):
            result["step_title"] = f"执行工作流：{skill_name}"
        return result
