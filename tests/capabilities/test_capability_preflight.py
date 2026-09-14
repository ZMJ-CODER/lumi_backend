"""Capability Preflight 契约回归（方案 §3.3 / §3.4）。

锁死：六项检查的**顺序**、八种状态、统一错误码映射、以及
"预检失败不调用主模型、不给空工具列表"这条硬约束。
"""

from __future__ import annotations

from app.agents.orchestration.preflight.capability_preflight import (
    STATUS_ERROR_CODES,
    PreflightStatus,
    preflight_capabilities,
    tool_window_for_actions,
)

ACTION_PROFILE = {
    "intent_type": "EXECUTE_ACTION",
    "action_intents": ["MODIFY"],
    "target_scope": "WORKSPACE",
    "target_clarity": "KNOWN",
    "required_capabilities": ["workspace.write"],
    "risk_level": "medium",
}


def _ready(**overrides):
    base = {
        "profile": ACTION_PROFILE,
        "workspace_bound": True,
        "requires_workspace": True,
        "available_capabilities": {"workspace.write": True},
        "provider_health": {"workspace.write": "healthy"},
        "registered_tools": {"workspace_navigator", "workspace_edit", "workspace_write"},
    }
    base.update(overrides)
    return preflight_capabilities(**base)


# ── 工具窗口（§3.4 画像驱动注入）────────────────────────


def test_tool_window_follows_action_intents():
    assert tool_window_for_actions(["READ"]) == ("workspace_navigator",)
    assert tool_window_for_actions(["MODIFY"]) == ("workspace_navigator", "workspace_edit")
    assert tool_window_for_actions(["DELETE"]) == ("workspace_navigator", "workspace_delete")
    assert tool_window_for_actions(["MOVE"]) == ("workspace_navigator", "workspace_move")
    assert tool_window_for_actions(["CREATE"]) == ("workspace_write",)
    # 工具名必须是**真实注册名**（否则预检"通过"却注入不存在的工具）。
    assert tool_window_for_actions(["EXECUTE"]) == ("run_in_sandbox", "python_exec")
    from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP

    for tool in tool_window_for_actions(["READ", "SEARCH", "CREATE", "MODIFY", "DELETE", "MOVE", "EXECUTE"]):
        assert tool in IMPLEMENTATION_MAP, f"{tool} 未注册"
    # 多意图合并去重、保序
    assert tool_window_for_actions(["READ", "MODIFY"]) == ("workspace_navigator", "workspace_edit")
    # 只暴露已注册的工具
    assert tool_window_for_actions(["MODIFY"], registered_tools={"workspace_edit"}) == ("workspace_edit",)
    assert tool_window_for_actions([]) == ()


# ── 检查顺序（安全策略 → 澄清 → 工作区 → 能力 → 权限 → 工具 → 审批）──


def test_ready_preflight_returns_tool_window_and_permits_model_call():
    result = _ready()
    assert result.ok and result.status == PreflightStatus.READY.value
    assert result.must_call_model is True
    assert result.tool_window == ("workspace_navigator", "workspace_edit")
    assert [check.name for check in result.checks] == [
        "security_policy", "target_clarity", "workspace_bound", "capability_available",
        "permission", "tool_window", "approval",
    ]
    assert result.error is None and result.error_code == ""


def test_security_policy_check_runs_before_everything():
    """硬拒优先：安全策略命中时连澄清都不问（不给可绕过的话术）。"""
    result = _ready(security_blocked=True, profile={**ACTION_PROFILE, "target_clarity": "UNKNOWN"})
    assert result.status == PreflightStatus.SECURITY_BLOCKED.value
    assert result.error_code == "SECURITY_BLOCKED"
    assert result.must_call_model is False
    assert result.permanent is True
    assert result.safe_next_action == "", "硬拒不给重试入口"
    assert result.checks[-1].name == "security_policy"


def test_safe_next_action_is_actionable_for_recoverable_failures():
    missing = _ready(workspace_bound=False)
    assert missing.safe_next_action == "BIND_WORKSPACE"
    clarify = _ready(profile={**ACTION_PROFILE, "target_clarity": "UNKNOWN"})
    assert clarify.safe_next_action == "PROVIDE_TARGET" and clarify.needs_human is True
    approval = _ready(approval_required=True)
    assert approval.safe_next_action == "APPROVE" and approval.needs_human is True
    denied = _ready(permissions={}, required_permission="workspace.write")
    assert denied.status == PreflightStatus.PERMISSION_DENIED.value
    assert denied.safe_next_action == "" and denied.permanent is True
    assert denied.required_capabilities == ("workspace.write",), "缺哪些能力要如实回传"


def test_clarification_comes_before_every_other_check():
    result = _ready(profile={**ACTION_PROFILE, "target_clarity": "UNKNOWN"}, workspace_bound=False)
    assert result.status == PreflightStatus.NEEDS_CLARIFICATION.value
    assert result.question, "必须给出澄清问题"
    assert not result.ok and result.must_call_model is False
    assert result.tool_window == (), "预检失败不给工具窗口"
    assert result.checks[-1].name == "target_clarity", "第一项检查即返回"


def test_workspace_check_runs_before_capability_check():
    result = _ready(workspace_bound=False, available_capabilities={"workspace.write": False})
    assert result.status == PreflightStatus.DEPENDENCY_MISSING.value
    assert result.error_code == "DEPENDENCY_MISSING_WORKSPACE"


def test_disabled_plugin_capability_is_unavailable():
    result = _ready(available_capabilities={"workspace.write": False})
    assert result.status == PreflightStatus.CAPABILITY_UNAVAILABLE.value
    assert result.error_code == "CAPABILITY_UNAVAILABLE"
    assert result.error is not None and result.error.safe_message
    assert result.must_call_model is False


def test_unhealthy_provider_is_distinguished_from_unavailable():
    result = _ready(provider_health={"workspace.write": "unhealthy"})
    assert result.status == PreflightStatus.PROVIDER_UNHEALTHY.value
    assert result.error_code == "PROVIDER_UNHEALTHY"
    assert result.error is not None and result.error.retryable is True, "Provider 不健康可重试"


def test_permission_denied():
    result = _ready(required_permission="workspace.write", permissions={"workspace.write": False})
    assert result.status == PreflightStatus.PERMISSION_DENIED.value
    assert result.error_code == "PERMISSION_DENIED"


def test_unregistered_tool_is_reported_by_code():
    result = _ready(desired_tools=["workspace_move"])
    assert result.status == PreflightStatus.TOOL_NOT_REGISTERED.value
    assert result.error_code == "TOOL_NOT_REGISTERED"
    assert "workspace_move" in result.checks[-1].detail


def test_action_without_injectable_tool_is_blocked_not_silently_downgraded():
    """插件/工具全被禁用时：阻断，而不是"空工具列表 + 纯文本瞎答"。"""
    result = _ready(registered_tools=set())
    assert result.status == PreflightStatus.CAPABILITY_UNAVAILABLE.value
    assert result.tool_window == ()
    assert result.must_call_model is False


def test_approval_required_is_last_check():
    result = _ready(approval_required=True)
    assert result.status == PreflightStatus.APPROVAL_REQUIRED.value
    assert result.error_code == "APPROVAL_REQUIRED"
    assert [check.name for check in result.checks][-1] == "approval"


def test_pure_generation_needs_no_tool_window():
    result = preflight_capabilities(profile={"intent_type": "GENERATE_ONLY", "action_intents": []})
    assert result.ok, "纯生成任务不应因为没有工具而被阻断"
    assert result.tool_window == ()


def test_every_status_maps_to_a_registered_error_code():
    from lumi_contracts import spec_for

    for status in PreflightStatus:
        code = STATUS_ERROR_CODES[status.value]
        if status is PreflightStatus.READY:
            assert code == ""
            continue
        assert code, status
        assert spec_for(code).code == code, f"{code} 未在统一错误模型里登记"


def test_result_is_serialisable_for_process_events():
    payload = _ready().as_dict()
    assert payload["status"] == "READY" and payload["ok"] is True
    assert isinstance(payload["checks"], list) and payload["checks"]
    failed = _ready(available_capabilities={"workspace.write": False}).as_dict()
    assert failed["error_code"] == "CAPABILITY_UNAVAILABLE"
    assert failed["safe_message"]

