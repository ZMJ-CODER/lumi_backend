"""SafetyGuard 的 app 适配：工具名/参数 → ExecutionEnv，并在工具执行前强校验。

策略本身在 ``lumi_orch.safety_policy``；本模块只负责环境判定与结果映射。
"""

from __future__ import annotations

from lumi_orch.safety_policy import (
    ExecutionEnv,
    SafetyAction,
    SafetyGuard,
)

_SANDBOX_PREFIXES = ("sandbox_",)
_DESKTOP_PREFIXES = (
    "workspace_", "mcp__lumi_pc__", "open_app", "open_file", "open_url",
    "desktop_", "local_",
)
_EXTERNAL_PREFIXES = ("send_email", "email_", "smtp_", "webhook_", "dingtalk_", "wecom_")

_BLOCK_MESSAGES = {
    "BLOCK": "该操作在当前位置被安全策略禁止（真机高风险或后端不允许）。",
    "ALLOW_SANDBOX_ONLY": "该操作仅允许在隔离沙箱内执行。",
}


def resolve_execution_env(tool_name: str, args: dict | None = None) -> ExecutionEnv:
    """由工具名与参数推断执行环境（沙箱/真机/后端/外部服务）。"""
    name = str(tool_name or "").strip().casefold()
    arguments = args or {}
    if any(name.startswith(prefix) for prefix in _SANDBOX_PREFIXES):
        return ExecutionEnv.SANDBOX
    if str(arguments.get("execution_env") or "").upper() == "SANDBOX":
        return ExecutionEnv.SANDBOX
    if any(name.startswith(prefix) for prefix in _DESKTOP_PREFIXES):
        return ExecutionEnv.DESKTOP
    if any(name.startswith(prefix) for prefix in _EXTERNAL_PREFIXES):
        return ExecutionEnv.EXTERNAL_SERVICE
    if name in {"run_shell", "shell", "bash", "powershell", "cmd"}:
        # 裸 shell 默认按真机对待（后端不允许直接执行）。
        return ExecutionEnv.DESKTOP
    return ExecutionEnv.BACKEND


def enforce_tool_safety(
    tool_call: dict,
    *,
    task_action: SafetyAction = SafetyAction.ALLOW,
    env: ExecutionEnv | str | None = None,
) -> tuple[bool, SafetyAction, str, str]:
    """执行前风控；返回 (allowed, action, message, error_code)。

    - BLOCK → 不允许执行；
    - ALLOW_SANDBOX_ONLY 且当前不是沙箱 → 不允许（提示需沙箱）；
    - REQUIRE_* 不在此处拦截（由既有审批门处理）。
    """
    function = (tool_call or {}).get("function") or {}
    tool = str(function.get("name") or (tool_call or {}).get("tool") or "")
    raw_args = function.get("arguments") or (tool_call or {}).get("arguments") or {}
    if isinstance(raw_args, str):
        import json

        try:
            raw_args = json.loads(raw_args or "{}")
        except (TypeError, ValueError):
            raw_args = {}
    args = raw_args if isinstance(raw_args, dict) else {}
    environment = env if isinstance(env, ExecutionEnv) else resolve_execution_env(tool, args)
    action = SafetyGuard.check(tool_call, task_action=task_action, env=environment)
    if action == SafetyAction.BLOCK:
        return False, action, _BLOCK_MESSAGES["BLOCK"], "SAFETY_BLOCKED"
    if action == SafetyAction.ALLOW_SANDBOX_ONLY and environment != ExecutionEnv.SANDBOX:
        return (
            False,
            action,
            _BLOCK_MESSAGES["ALLOW_SANDBOX_ONLY"],
            "SAFETY_SANDBOX_REQUIRED",
        )
    return True, action, "", ""


def requires_approval(action: SafetyAction) -> bool:
    return action in {SafetyAction.REQUIRE_USER_APPROVAL, SafetyAction.REQUIRE_ADMIN_APPROVAL}


__all__ = [
    "enforce_tool_safety",
    "requires_approval",
    "resolve_execution_env",
]
