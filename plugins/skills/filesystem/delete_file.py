"""技能插件（filesystem/文件系统操作）：delete_file —— 删除本地文件/目录（移入回收站）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class DeleteFileSkill(Skill):
    name = "delete_file"
    description = (
        "删除用户电脑上的文件或目录（移入回收站，可恢复）。"
        "默认需要用户确认。仅当用户在当前轮明确要求删除该单个文件时，执行器才可免确认。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要删除的文件或目录的绝对路径"},
            "recursive": {"type": "boolean", "description": "删除目录时是否递归（默认 false，仅文件）"},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        path = str(params.get("path") or "").strip()
        if not path:
            return SkillResult(success=False, error="缺少路径 path", error_code="INVALID_ARGS", retryable=False)
        _notify(context, f"（正在请求删除：{path}）")
        # The fallback request queue cannot trust tool parameters either.  The
        # executor alone may place this narrow, current-turn grant in context.
        explicit_user_delete = bool(
            context.execution_policy and context.execution_policy.get("explicit_user_delete")
        )
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {"path": path, "recursive": bool(params.get("recursive"))},
            not explicit_user_delete,
        )
