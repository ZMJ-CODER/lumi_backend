"""命令会话基础工具。"""

from app.agents.skills.base import SkillContext, Tool, ToolOutput
from app.agents.skills.executor import run_client_skill_request


async def _client(name: str, params: dict, context: SkillContext | None, confirm: bool = False) -> ToolOutput:
    if not context or not context.user_id:
        return ToolOutput(success=False, error="需要登录后使用", error_code="AUTH_REQUIRED", retryable=False)
    return await run_client_skill_request(context.user_id, name, params, confirm)


class BashTool(Tool):
    name = "Bash"
    description = "在用户确认后执行受控命令；支持前台执行和后台持久会话。"
    category = "shell"
    domain = "system"
    resource = "process"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
            "run_in_background": {"type": "boolean"}, "cwd": {"type": "string"},
        },
        "required": ["command"],
    }
    use_when = ["用户明确要求执行命令或脚本"]
    do_not_use_when = ["可用专用工具完成时不要执行任意命令", "命令未获用户授权"]
    result_contract = "返回命令输出，后台任务返回 shell_id。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        command = str(params.get("command") or "").strip()
        if not command:
            return ToolOutput(success=False, error="缺少 command", error_code="INVALID_ARGS", retryable=False)
        payload = {"command": command, "timeout": int(params.get("timeout") or 30), "cwd": str(params.get("cwd") or ""), "run_in_background": bool(params.get("run_in_background"))}
        if params.get("project_id"):
            payload["project_id"] = str(params["project_id"])
        return await _client("Bash", payload, context, True)


class BashOutputTool(Tool):
    name = "BashOutput"
    description = "读取后台 Bash 会话自上次检查以来的新输出。"
    category = "shell"
    domain = "system"
    resource = "process"
    environment = "client"
    parameters_schema = {"type": "object", "properties": {"bash_id": {"type": "string"}, "filter": {"type": "string"}}, "required": ["bash_id"]}
    use_when = ["用户要求查看后台命令输出"]
    do_not_use_when = ["没有有效的后台会话 ID"]
    result_contract = "返回新增 stdout/stderr 和进程状态。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        sid = str(params.get("bash_id") or "").strip()
        if not sid:
            return ToolOutput(success=False, error="缺少 bash_id", error_code="INVALID_ARGS", retryable=False)
        return await _client("BashOutput", {"bash_id": sid, "filter": str(params.get("filter") or "")}, context)


class KillShellTool(Tool):
    name = "KillShell"
    description = "终止指定的后台 Bash 会话；需要用户确认。"
    category = "shell"
    domain = "system"
    resource = "process"
    environment = "client"
    requires_confirmation = True
    write_op = True
    parameters_schema = {"type": "object", "properties": {"shell_id": {"type": "string"}}, "required": ["shell_id"]}
    use_when = ["用户明确要求终止后台命令"]
    do_not_use_when = ["未指定 shell_id"]
    result_contract = "返回终止状态。"

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        sid = str(params.get("shell_id") or "").strip()
        if not sid:
            return ToolOutput(success=False, error="缺少 shell_id", error_code="INVALID_ARGS", retryable=False)
        return await _client("KillShell", {"shell_id": sid}, context, True)
