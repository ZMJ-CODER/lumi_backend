"""SafetyGuard app 适配与工具级强校验的回归测试。

覆盖：环境判定、沙箱/真机差异化决策、执行入口前置拦截。
"""

from __future__ import annotations

import asyncio

from lumi_orch.safety_policy import ExecutionEnv, SafetyAction

from app.services.safety_adapter import (
    enforce_tool_safety,
    requires_approval,
    resolve_execution_env,
)


def test_resolve_execution_env_by_tool_and_args():
    assert resolve_execution_env("sandbox_run_code", {}) == ExecutionEnv.SANDBOX
    assert resolve_execution_env("workspace_read", {}) == ExecutionEnv.DESKTOP
    assert resolve_execution_env("mcp__lumi_pc__workspace_commit", {}) == ExecutionEnv.DESKTOP
    assert resolve_execution_env("send_email", {}) == ExecutionEnv.EXTERNAL_SERVICE
    assert resolve_execution_env("Calculator", {}) == ExecutionEnv.BACKEND
    # 显式声明沙箱
    assert resolve_execution_env("run_shell", {"execution_env": "SANDBOX"}) == ExecutionEnv.SANDBOX
    # 裸 shell 默认真机
    assert resolve_execution_env("run_shell", {}) == ExecutionEnv.DESKTOP


def test_enforce_tool_safety_matrix():
    allowed, action, message, code = enforce_tool_safety(
        {"function": {"name": "run_shell", "arguments": {"command": "rm -rf /"}}},
    )
    assert not allowed and action == SafetyAction.BLOCK and code == "SAFETY_BLOCKED"
    assert "禁止" in message

    allowed2, action2, _, code2 = enforce_tool_safety(
        {"function": {"name": "run_shell", "arguments": {"command": "ls"}}},
        env=ExecutionEnv.BACKEND,
    )
    assert not allowed2 and action2 == SafetyAction.BLOCK

    allowed3, action3, _, _ = enforce_tool_safety(
        {"function": {"name": "sandbox_run_code", "arguments": {"code": "print(1)"}}},
        env=ExecutionEnv.SANDBOX,
    )
    assert allowed3 and action3 == SafetyAction.ALLOW_SANDBOX_ONLY
    assert not requires_approval(action3)

    allowed4, action4, _, _ = enforce_tool_safety(
        {"function": {"name": "send_email", "arguments": {"to": "x@y.z"}}},
        env=ExecutionEnv.BACKEND,
    )
    assert allowed4 and action4 == SafetyAction.REQUIRE_USER_APPROVAL
    assert requires_approval(action4)


def test_execute_tool_call_blocks_before_tool_lookup(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", True)
    from app.agents.skills.executor import execute_tool_call

    async def scenario():
        result = await execute_tool_call(
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "run_shell", "arguments": {"command": "rm -rf /"}},
            },
            "u1",
            "office",
            "c1",
        )
        return result

    result = asyncio.run(scenario())
    assert result.success is False
    assert result.error_code == "SAFETY_BLOCKED"
    assert result.metadata.get("safety_action") == SafetyAction.BLOCK.value


def test_execute_tool_call_no_enforcement_when_flag_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", False)
    from app.agents.skills.executor import execute_tool_call

    async def scenario():
        return await execute_tool_call(
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "完全不存在的工具", "arguments": {}},
            },
            "u1",
            "office",
            "c1",
        )

    result = asyncio.run(scenario())
    # 关闭开关时不应被风控拦截，而是走原有未注册工具路径
    assert result.error_code != "SAFETY_BLOCKED"
