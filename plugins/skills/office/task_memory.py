"""办公技能（office/记忆）：task_memory —— 任务内工作记忆（记录/回顾已读文件与决策）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.memory.task_memory import format_memory, recall, remember


class TaskMemorySkill(Skill):
    name = "task_memory"
    description = (
        "任务内工作记忆：remember 记录关键事实/已读文件/已做决策，recall 回顾本任务已记录的内容。"
        "多步骤任务中后续步骤应先用 recall 查看前面步骤的产出，避免重复读取或丢失上下文。"
    )
    category = "office"
    environment = "server"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "remember（记录）或 recall（回顾）"},
            "key": {"type": "string", "description": "记录项名称，如 已读文件/决策"},
            "value": {"type": "string", "description": "记录内容（remember 必填）"},
        },
        "required": ["action"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        action = str(params.get("action") or "").strip().lower()
        job_id = str(context.conversation_id or "")
        if not job_id:
            return SkillResult(success=False, error="缺少任务上下文", error_code="EXEC_ERROR", retryable=False)
        if action == "remember":
            key = str(params.get("key") or "").strip()
            value = str(params.get("value") or "").strip()
            if not key or not value:
                return SkillResult(success=False, error="remember 需要 key 和 value", error_code="INVALID_ARGS", retryable=False)
            await remember(job_id, key, value)
            return SkillResult(success=True, output=f"已记录任务记忆：{key}")
        if action == "recall":
            mem = await recall(job_id)
            return SkillResult(
                success=True,
                output=format_memory(mem) or "（任务内暂无记忆）",
                metadata={"memory": mem},
            )
        return SkillResult(success=False, error="action 仅支持 remember/recall", error_code="INVALID_ARGS", retryable=False)
