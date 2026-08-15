"""技能插件（desktop/GUI与桌面控制）：open_file —— 用系统默认应用打开文件.

打开文件/目录属于"桌面控制"类操作（操作系统默认应用），
执行通道（混合架构）：配置 MCP 时由后端直连 Electron 端 MCP server 执行
（高危弹窗确认在 Electron 主进程内完成）；未配置时回退 Redis 轮询弹窗确认。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class OpenFileSkill(Skill):
    name = "open_file"
    description = (
        "用系统默认应用打开用户电脑上的文件（文档、图片、程序等）。"
        "高危操作：执行前需要用户在客户端确认。"
    )
    category = "desktop"
    environment = "client"
    requires_confirmation = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要打开的文件绝对路径"},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        path = str(params.get("path") or "").strip()
        if not path:
            return SkillResult(success=False, error="缺少文件路径 path", error_code="INVALID_ARGS", retryable=False)
        _notify(context, f"（正在请求打开本地文件：{path}，请在弹出的确认框中确认）")
        user_id = context.user_id if context else ""
        return await run_client_skill_request(
            user_id,
            self.name,
            {"path": path},
            requires_confirmation=True,
        )
