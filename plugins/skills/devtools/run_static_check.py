"""技能插件（devtools/开发工具链）：run_static_check —— 轻量级静态类型/语法检查."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class RunStaticCheckSkill(Skill):
    name = "run_static_check"
    description = (
        "在本地项目运行轻量级静态类型/语法检查（tsc / vue-tsc / eslint / py_compile），"
        "快速发现编译级错误，比完整构建快得多。测试/构建前先跑它；"
        "通过后完整测试异步进行，不再阻塞任务。"
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
        _notify(context, "（正在运行静态类型检查…）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id},
            False,
        )
