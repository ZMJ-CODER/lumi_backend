"""技能插件（filesystem/文件系统操作）：search_files —— 按文件名/通配符搜索本地文件."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class SearchFilesSkill(Skill):
    name = "search_files"
    description = (
        "在用户电脑的某个目录（含子目录）中按文件名/通配符搜索文件。"
        "当用户忘记文件放哪、或需要按名字查找文件时使用。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "起始目录绝对路径"},
            "pattern": {"type": "string", "description": "文件名匹配（支持 * 通配符，如 *.pdf / report*）"},
            "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件（默认 false）"},
            "max_results": {"type": "integer", "description": "最多返回条数（默认 50）", "minimum": 1, "maximum": 200},
        },
        "required": ["path", "pattern"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        path = str(params.get("path") or "").strip()
        pattern = str(params.get("pattern") or "").strip()
        if not path or not pattern:
            return SkillResult(
                success=False,
                error="缺少 path 或 pattern",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在搜索本地文件：{pattern} ← {path}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "path": path,
                "pattern": pattern,
                "include_hidden": bool(params.get("include_hidden")),
                "max_results": int(params.get("max_results") or 50),
            },
            False,
        )
