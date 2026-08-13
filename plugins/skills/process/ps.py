"""技能插件（process/进程管理）：ps —— 列出用户电脑上的进程."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class PsSkill(Skill):
    name = "ps"
    description = (
        "列出用户电脑上正在运行的进程（进程名、PID、内存占用）。"
        "可按进程名过滤。当用户想查看/定位某个程序是否在运行时使用。"
    )
    category = "process"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "可选：按进程名模糊过滤"},
            "max_results": {"type": "integer", "description": "最多返回条数（默认 50）", "minimum": 1, "maximum": 200},
        },
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        _notify(context, "（正在读取进程列表）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "pattern": str(params.get("pattern") or ""),
                "max_results": int(params.get("max_results") or 50),
            },
            False,
        )
