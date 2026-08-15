"""技能插件（filesystem/文件系统操作）：rename_file —— 重命名/移动本地文件."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class RenameFileSkill(Skill):
    name = "rename_file"
    description = (
        "重命名或移动用户电脑上的文件/目录（绝对路径）。"
        "当需要改文件名、移动文件位置时使用。可恢复（改回即可），无需逐次确认。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "原文件/目录绝对路径"},
            "new_path": {"type": "string", "description": "新文件/目录绝对路径"},
        },
        "required": ["path", "new_path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        path = str(params.get("path") or "").strip()
        new_path = str(params.get("new_path") or "").strip()
        if not path or not new_path:
            return SkillResult(
                success=False,
                error="缺少 path 或 new_path",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在重命名：{path} → {new_path}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"path": path, "new_path": new_path},
            False,
        )
