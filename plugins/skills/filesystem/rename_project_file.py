"""技能插件（filesystem/文件系统操作）：rename_project_file —— 重命名/移动项目内文件."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class RenameProjectFileSkill(Skill):
    name = "rename_project_file"
    description = (
        "重命名或移动本地代码项目中的文件（相对项目根，自动创建目标目录）。"
        "当需要改文件名、整理目录结构时使用。可恢复，无需逐次确认。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "path": {"type": "string", "description": "原文件相对项目根路径"},
            "new_path": {"type": "string", "description": "新文件相对项目根路径"},
        },
        "required": ["project_id", "path", "new_path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        path = str(params.get("path") or "").strip()
        new_path = str(params.get("new_path") or "").strip()
        if not project_id or not path or not new_path:
            return SkillResult(
                success=False,
                error="缺少 project_id / path / new_path",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在重命名项目文件：{path} → {new_path}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id, "path": path, "new_path": new_path},
            False,
        )
