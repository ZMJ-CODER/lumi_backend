"""技能插件（devtools/开发工具链）：extract_code_blocks —— 本地提取代码块（大文件不全文回传）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class ExtractCodeBlocksSkill(Skill):
    name = "extract_code_blocks"
    description = (
        "在本地提取代码文件的相关代码块（tree-sitter 函数/类定义）与引用导入。"
        "小文件返回全文，大文件只返回相关块，不把全文传回服务端。"
        "大文件改动前先调用本工具获取编辑上下文。"
    )
    category = "devtools"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "path": {"type": "string", "description": "相对项目根的文件路径"},
            "instruction": {"type": "string", "description": "用户指令（用于相关性排序）"},
            "max_blocks": {"type": "integer", "description": "最多返回块数（默认 8）", "minimum": 1, "maximum": 30},
            "context_lines": {"type": "integer", "description": "每个代码块前后附加的上下文行数（默认 10）", "minimum": 0, "maximum": 50},
        },
        "required": ["project_id", "path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        path = str(params.get("path") or "").strip()
        if not project_id or not path:
            return SkillResult(
                success=False,
                error="缺少 project_id 或 path",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在本地提取代码块：{path}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "project_id": project_id,
                "path": path,
                "instruction": str(params.get("instruction") or ""),
                "max_blocks": int(params.get("max_blocks") or 8),
                "context_lines": int(params.get("context_lines") or 10),
            },
            False,
        )
