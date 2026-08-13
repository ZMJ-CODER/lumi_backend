"""技能插件（filesystem/文件系统操作）：file_stat —— 获取本地文件/目录状态."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class FileStatSkill(Skill):
    name = "file_stat"
    description = (
        "获取用户电脑上文件或目录的状态信息（大小、类型、修改时间、创建时间等）。"
        "当需要判断文件是否存在、大小、新旧程度时使用。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录的绝对路径"},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        path = str(params.get("path") or "").strip()
        if not path:
            return SkillResult(success=False, error="缺少路径 path", error_code="INVALID_ARGS", retryable=False)
        _notify(context, f"（正在查看文件状态：{path}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"path": path},
            False,
        )
