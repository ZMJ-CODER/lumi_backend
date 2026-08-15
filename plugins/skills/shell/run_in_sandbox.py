"""技能插件（shell/终端执行）：run_in_sandbox —— 在本地沙箱副本中执行命令（隔离）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class RunInSandboxSkill(Skill):
    name = "run_in_sandbox"
    description = (
        "在本地项目的沙箱副本中执行命令（测试/构建/脚本），"
        "暂存修改会先应用到沙箱副本，命令不会触碰真实项目文件。"
        "用于运行测试、构建验证、执行脚本等需要真实执行但应隔离的场景。"
    )
    category = "shell"
    environment = "client"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "command": {"type": "string", "description": "要执行的命令，如 npm test / npm run build / pytest -q"},
            "timeout": {"type": "integer", "description": "超时秒数（默认 60，最大 600）", "minimum": 1, "maximum": 600},
            "cwd": {"type": "string", "description": "可选：沙箱内相对目录"},
        },
        "required": ["project_id", "command"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        command = str(params.get("command") or "").strip()
        if not project_id or not command:
            return SkillResult(
                success=False,
                error="缺少 project_id 或 command",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在沙箱中执行：{command[:80]}）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "project_id": project_id,
                "command": command,
                "timeout": int(params.get("timeout") or 60),
                "cwd": str(params.get("cwd") or ""),
            },
            False,
        )
