"""SafetyGuard：两层风控（任务级预检 + 工具级强校验）与 SafetyAction 策略。

不依赖 app/IO：工具调用以 dict 传入，环境以 ``ExecutionEnv`` 显式给出。
"""

from __future__ import annotations

import re
from enum import Enum

from lumi_orch.task_assessment import TaskProfile, has_side_effects


class SafetyAction(str, Enum):
    ALLOW = "ALLOW"
    ALLOW_SANDBOX_ONLY = "ALLOW_SANDBOX_ONLY"
    REQUIRE_USER_APPROVAL = "REQUIRE_USER_APPROVAL"
    REQUIRE_ADMIN_APPROVAL = "REQUIRE_ADMIN_APPROVAL"
    BLOCK = "BLOCK"


class ExecutionEnv(str, Enum):
    SANDBOX = "SANDBOX"
    DESKTOP = "DESKTOP"
    BACKEND = "BACKEND"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"


_SEVERITY = {
    SafetyAction.ALLOW: 0,
    SafetyAction.ALLOW_SANDBOX_ONLY: 1,
    SafetyAction.REQUIRE_USER_APPROVAL: 2,
    SafetyAction.REQUIRE_ADMIN_APPROVAL: 3,
    SafetyAction.BLOCK: 4,
}

_SHELL_TOOLS = frozenset({
    "run_shell", "shell", "bash", "sh", "powershell", "cmd",
    "sandbox_run", "sandbox_exec", "sandbox_run_code", "execute_command",
})
_EMAIL_TOOLS = frozenset({"send_email", "smtp_send", "mail_send", "email_send", "compose_email"})
_DELETE_TOOLS = frozenset({"delete_file", "workspace_stage_delete", "remove_file", "rm"})
_WRITE_TOOLS = frozenset({
    "write_file", "workspace_stage_write", "save_file", "edit_file", "apply_patch",
})

_DANGEROUS_SHELL = re.compile(
    r"(?i)(rm\s+-rf\s+/|rm\s+-rf\s+~|mkfs|dd\s+if=|format\s+[a-z]:|del\s+/[sf]|"
    r":\(\)\s*\{|shutdown|reboot|diskpart|drop\s+database)"
)


def combine(action_a: SafetyAction, action_b: SafetyAction) -> SafetyAction:
    """取更严格的动作（ALLOW_SANDBOX_ONLY 视为比 ALLOW 严格的受限放行）。"""
    return action_a if _SEVERITY[action_a] >= _SEVERITY[action_b] else action_b


def task_level_action(profile: TaskProfile) -> SafetyAction:
    """Layer 1：任务级预检（基于 risk_level 与 side_effects）。"""
    side_effects = set(profile.side_effects)
    if profile.risk_level == "HIGH_RISK":
        return SafetyAction.REQUIRE_ADMIN_APPROVAL
    if profile.risk_level == "REQUIRES_APPROVAL":
        return SafetyAction.REQUIRE_USER_APPROVAL
    if side_effects & {"SEND", "PUBLISH"}:
        return SafetyAction.REQUIRE_USER_APPROVAL
    if profile.execution_target == "DESKTOP" and side_effects:
        # 真实文件写入：暂存 → Diff → 前端审批。
        return SafetyAction.REQUIRE_USER_APPROVAL
    if has_side_effects(profile):
        return SafetyAction.ALLOW
    return SafetyAction.ALLOW


def tool_level_action(
    *,
    tool: str,
    args: dict | None = None,
    env: ExecutionEnv | str = ExecutionEnv.BACKEND,
    task_action: SafetyAction = SafetyAction.ALLOW,
) -> SafetyAction:
    """Layer 2：工具级强校验（每次调用前重新计算，环境敏感）。"""
    name = str(tool or "").strip().casefold()
    arguments = args or {}
    environment = ExecutionEnv(env) if not isinstance(env, ExecutionEnv) else env
    action = SafetyAction.ALLOW

    if name in _SHELL_TOOLS:
        command = str(arguments.get("command") or arguments.get("code") or "")
        dangerous = bool(_DANGEROUS_SHELL.search(command)) or bool(arguments.get("destructive"))
        if environment == ExecutionEnv.SANDBOX:
            # 沙箱内允许执行/删除；危险命令也仅在沙箱内可回滚。
            action = SafetyAction.ALLOW_SANDBOX_ONLY
        elif environment == ExecutionEnv.DESKTOP:
            action = SafetyAction.BLOCK if dangerous else SafetyAction.REQUIRE_USER_APPROVAL
        else:
            # 后端不允许直接跑 shell。
            action = SafetyAction.BLOCK
    elif name in _DELETE_TOOLS:
        if bool(arguments.get("use_trash")):
            action = SafetyAction.ALLOW
        else:
            action = (
                SafetyAction.BLOCK
                if environment == ExecutionEnv.DESKTOP
                else SafetyAction.REQUIRE_ADMIN_APPROVAL
            )
    elif name in _EMAIL_TOOLS:
        action = SafetyAction.REQUIRE_USER_APPROVAL
    elif name in _WRITE_TOOLS:
        action = (
            SafetyAction.REQUIRE_USER_APPROVAL
            if environment == ExecutionEnv.DESKTOP
            else SafetyAction.ALLOW
        )
    return combine(task_action, action)


class SafetyException(RuntimeError):
    """工具调用被风控拒绝时抛出（调用方据此回填错误，不得静默重试）。"""


class SafetyGuard:
    """执行器调用的风控入口：check → 决策；enforce → 拒绝即抛异常。"""

    @staticmethod
    def check(
        tool_call: dict,
        *,
        task_action: SafetyAction = SafetyAction.ALLOW,
        env: ExecutionEnv | str = ExecutionEnv.BACKEND,
    ) -> SafetyAction:
        function = (tool_call or {}).get("function") or {}
        tool = str(function.get("name") or (tool_call or {}).get("tool") or "")
        raw_args = function.get("arguments") or (tool_call or {}).get("arguments") or {}
        if isinstance(raw_args, str):
            import json

            try:
                raw_args = json.loads(raw_args or "{}")
            except (TypeError, ValueError):
                raw_args = {}
        return tool_level_action(
            tool=tool,
            args=raw_args if isinstance(raw_args, dict) else {},
            env=env,
            task_action=task_action,
        )

    @classmethod
    def enforce(
        cls,
        tool_call: dict,
        *,
        task_action: SafetyAction = SafetyAction.ALLOW,
        env: ExecutionEnv | str = ExecutionEnv.BACKEND,
    ) -> SafetyAction:
        action = cls.check(tool_call, task_action=task_action, env=env)
        if action == SafetyAction.BLOCK:
            raise SafetyException("Action blocked: high risk detected")
        return action


__all__ = [
    "ExecutionEnv",
    "SafetyAction",
    "SafetyException",
    "SafetyGuard",
    "combine",
    "task_level_action",
    "tool_level_action",
]
