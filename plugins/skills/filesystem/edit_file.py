"""技能插件（filesystem/文件系统操作）：edit_file —— 编辑本地文件（替换指定内容）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class EditFileSkill(Skill):
    name = "edit_file"
    description = (
        "在用户电脑的文本文件中用新内容替换指定片段（先读取确认原文，再精确替换）。"
        "比整文件覆盖更安全，适合小范围修改。高危操作：执行前需要用户确认。"
    )
    category = "filesystem"
    environment = "client"
    requires_confirmation = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要编辑的文件绝对路径"},
            "old_text": {"type": "string", "description": "要被替换的原文片段（须与文件内容完全一致）"},
            "new_text": {"type": "string", "description": "替换后的新内容"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认 false，只替换第一处）"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        path = str(params.get("path") or "").strip()
        old_text = str(params.get("old_text") or "")
        new_text = str(params.get("new_text") or "")
        if not path or not old_text:
            return SkillResult(
                success=False,
                error="缺少 path 或 old_text",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在请求编辑本地文件：{path}，请在弹出的确认框中确认）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "path": path,
                "old_text": old_text,
                "new_text": new_text,
                "replace_all": bool(params.get("replace_all")),
            },
            True,
        )
