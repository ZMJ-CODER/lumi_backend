"""技能插件（shell/终端执行）：bash —— 在用户电脑上执行 shell 命令（带超时与输出限制）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class BashSkill(Skill):
    name = "bash"
    description = (
        "在用户电脑上执行任意 shell 命令（Windows 用 cmd，Linux/macOS 用 bash），"
        "带超时与输出截断。用于运行项目命令、脚本、检查环境等。"
        "高危操作：任意命令可能修改系统，执行前需要用户确认。"
    )
    category = "shell"
    environment = "client"
    requires_confirmation = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的完整命令"},
            "timeout": {"type": "integer", "description": "超时秒数（默认 30，最大 300）", "minimum": 1, "maximum": 300},
            "cwd": {"type": "string", "description": "可选：执行目录（绝对路径）"},
        },
        "required": ["command"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        command = str(params.get("command") or "").strip()
        if not command:
            return SkillResult(success=False, error="缺少命令 command", error_code="INVALID_ARGS", retryable=False)
        _notify(context, f"（正在请求执行命令：{command[:80]}，请在弹出的确认框中确认）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "command": command,
                "timeout": int(params.get("timeout") or 30),
                "cwd": str(params.get("cwd") or ""),
            },
            True,
        )
