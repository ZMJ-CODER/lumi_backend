"""技能插件（devtools/开发工具链）：grep_code —— 按正则搜索本地项目代码内容."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class GrepCodeSkill(Skill):
    name = "grep_code"
    description = (
        "在本地代码项目中按正则搜索文件内容（直接搜本地文件系统，不依赖索引/向量），"
        "返回 文件:行号:代码片段。需要定位某段代码、函数、关键词、报错出处时使用。"
    )
    category = "devtools"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "pattern": {"type": "string", "description": "正则表达式（忽略大小写），如 login|auth、function\\s+\\w+"},
            "path": {"type": "string", "description": "可选：限制搜索的相对目录"},
            "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件/目录（默认 false）"},
            "max_results": {"type": "integer", "description": "最多返回条数（默认 30）", "minimum": 1, "maximum": 100},
        },
        "required": ["project_id", "pattern"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        pattern = str(params.get("pattern") or "").strip()
        if not project_id or not pattern:
            return SkillResult(
                success=False,
                error="缺少 project_id 或 pattern",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在搜索代码：{pattern[:60]}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "project_id": project_id,
                "pattern": pattern,
                "path": str(params.get("path") or ""),
                "include_hidden": bool(params.get("include_hidden")),
                "max_results": int(params.get("max_results") or 30),
            },
            False,
        )
