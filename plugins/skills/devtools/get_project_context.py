"""技能插件（devtools/开发工具链）：get_project_context —— 读取本地项目热缓存记忆上下文."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class GetProjectContextSkill(Skill):
    name = "get_project_context"
    description = (
        "读取本地项目的记忆上下文（技术栈、入口文件、最近修改的文件、项目约定、当前任务状态）。"
        "开始读写代码前先调用，避免重复扫描项目或重新向量检索；"
        "涉及\"最近改的/上次改的/继续修改\"等指令时优先从返回的最近修改列表中定位文件。"
    )
    category = "devtools"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
        },
        "required": ["project_id"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        if not project_id:
            return SkillResult(success=False, error="缺少 project_id", error_code="INVALID_ARGS", retryable=False)
        _notify(context, "（正在读取项目记忆上下文）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id},
            False,
        )
