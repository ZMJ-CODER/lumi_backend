"""技能插件（desktop/GUI与桌面控制）：open_file —— 用系统默认应用打开文件.

打开文件/目录属于"桌面控制"类操作（操作系统默认应用），
执行通道：客户端（Electron）弹窗确认后调用系统默认应用打开。
"""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.services import client_tools


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
        req = await client_tools.create_client_tool_request(
            user_id, self.name, {"path": path}, True
        )
        if not req:
            return SkillResult(
                success=False,
                error="本地文件技能需要登录后使用",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        result = await client_tools.await_result(user_id, req["request_id"])
        if result is None:
            return SkillResult(
                success=False,
                error="等待用户响应超时，操作已取消",
                error_code="TIMEOUT",
                retryable=False,
            )
        if result.get("success"):
            return SkillResult(
                success=True,
                output=str(result.get("output") or ""),
                metadata=result.get("metadata") or {},
            )
        return SkillResult(
            success=False,
            error=str(result.get("error") or "客户端执行失败"),
            error_code=str((result.get("metadata") or {}).get("error_code") or "EXEC_ERROR"),
            retryable=False,
            metadata=result.get("metadata") or {},
        )
