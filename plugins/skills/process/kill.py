"""技能插件（process/进程管理）：kill —— 结束用户电脑上的进程."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class KillSkill(Skill):
    name = "kill"
    description = (
        "结束用户电脑上的指定进程（按 PID 或进程名）。"
        "高危操作：会强制终止程序，可能导致未保存数据丢失，执行前必须用户确认。"
    )
    category = "process"
    environment = "client"
    requires_confirmation = True
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "进程 PID（与 name 二选一）"},
            "name": {"type": "string", "description": "进程名（如 notepad.exe，与 pid 二选一）"},
            "force": {"type": "boolean", "description": "强制结束（默认 true）"},
        },
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        pid = params.get("pid")
        name = str(params.get("name") or "").strip()
        if pid is None and not name:
            return SkillResult(
                success=False,
                error="需要提供 pid 或 name",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        _notify(context, f"（正在请求结束进程：{name or pid}，请在弹出的确认框中确认）")
        return await run_client_skill_request(
            context.user_id,
            self.name,
            {
                "pid": int(pid) if pid is not None else None,
                "name": name,
                "force": bool(params.get("force", True)),
            },
            True,
        )
