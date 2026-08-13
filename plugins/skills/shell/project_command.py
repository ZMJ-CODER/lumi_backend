"""技能插件（shell/终端执行）：run_project_command —— 在本地项目根执行测试/构建命令.

命令受白名单（npm/pnpm/yarn/pytest/cargo/go/make 等）与项目根目录 jail 约束，
无需逐次确认（白名单内）。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class RunProjectCommandSkill(Skill):
    name = "run_project_command"
    description = (
        "在本地代码项目根目录执行测试/构建命令（白名单：npm/pnpm/yarn/pytest/cargo/go/make 等）。"
        "用于运行测试或构建验证。命令受白名单与项目根目录 jail 约束，无需逐次确认。"
    )
    category = "shell"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "command": {"type": "string", "description": "要执行的命令，如 npm test / pytest / cargo test"},
        },
        "required": ["project_id", "command"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        command = str(params.get("command") or "").strip()
        _notify(context, f"（正在项目中执行：{command}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id, "command": command},
            False,
        )
