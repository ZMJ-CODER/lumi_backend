"""技能插件（devtools/开发工具链）：apply_patch —— 本地应用 SEARCH/REPLACE 补丁（暂存）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class ApplyPatchSkill(Skill):
    name = "apply_patch"
    description = (
        "在本地文件上应用 SEARCH/REPLACE 补丁（写入暂存缓冲，不落盘）。"
        "每个块格式：{\"old\": 文件原文（逐字符一致）, \"new\": 新内容}；"
        "匹配失败会返回具体原因。配合 extract_code_blocks 使用。"
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
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                },
                "description": "SEARCH/REPLACE 块列表",
            },
            "reset_file": {
                "type": "boolean",
                "description": "重试时置 true：基于原始文件重新应用，避免叠加在上一轮补丁上",
            },
        },
        "required": ["project_id", "path", "blocks"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        path = str(params.get("path") or "").strip()
        blocks = params.get("blocks")
        if not project_id or not path or not isinstance(blocks, list) or not blocks:
            return SkillResult(
                success=False,
                error="缺少 project_id / path / blocks",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在本地应用补丁：{path}，共 {len(blocks)} 块）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "project_id": project_id,
                "path": path,
                "blocks": blocks,
                "reset_file": bool(params.get("reset_file")),
            },
            False,
        )
