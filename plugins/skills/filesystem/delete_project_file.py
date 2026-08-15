"""技能插件（filesystem/文件系统操作）：delete_project_file —— 删除项目内文件/目录（移入回收站）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class DeleteProjectFileSkill(Skill):
    name = "delete_project_file"
    description = (
        "删除本地代码项目中的文件或目录（相对项目根，移入回收站可恢复）。"
        "当 agent 创建临时脚本/缓存运行后需要清理，或删除废弃文件时使用。"
        "删除移入回收站可恢复，无需逐次确认。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "path": {"type": "string", "description": "相对项目根的文件/目录路径"},
            "recursive": {"type": "boolean", "description": "删除目录时是否递归（默认 false）"},
        },
        "required": ["project_id", "path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        rel = str(params.get("path") or "").strip()
        if not project_id or not rel:
            return SkillResult(
                success=False,
                error="缺少 project_id 或 path",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在请求删除项目文件：{rel}，请在弹出的确认框中确认）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"project_id": project_id, "path": rel, "recursive": bool(params.get("recursive"))},
            False,
        )
