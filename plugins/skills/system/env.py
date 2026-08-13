"""技能插件（system/系统信息与硬件）：env —— 读取用户电脑的环境变量."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class EnvSkill(Skill):
    name = "env"
    description = (
        "读取用户电脑的环境变量（如 PATH、HOME、JAVA_HOME 等）。"
        "当需要判断某个工具是否已安装、路径配置是否正确时使用。"
    )
    category = "system"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：只返回这些变量名；缺省返回全部",
            }
        },
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        _notify(context, "（正在读取环境变量）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"keys": list(params.get("keys") or [])},
            False,
        )
