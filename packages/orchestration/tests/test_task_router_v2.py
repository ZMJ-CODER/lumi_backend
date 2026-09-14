"""修订版任务画像 / Router 8 步 / SafetyGuard / 升级决策树 包级回归。

覆盖用户方案中的关键场景与硬约束：
  1) 纯文本处理 → DIRECT_CHAT；
  2) 工作区只读 → M1_ATOMIC_READ；
  3) 沙箱执行 → M1_ATOMIC_ACTION + ALLOW_SANDBOX_ONLY；
  4) 真实文件修改 → M1_ATOMIC_ACTION + 前端审批；
  并断言：side_effects 非空绝不进入 M0/M1_ATOMIC_READ；CONTEXT_TOO_LARGE
  不升级；依赖缺失/安全违规会被拦截。
"""

from __future__ import annotations

import pytest

from lumi_orch.execution_router import ExecutionMode, route
from lumi_orch.safety_policy import (
    ExecutionEnv,
    SafetyAction,
    SafetyGuard,
    SafetyException,
    task_level_action,
    tool_level_action,
)
from lumi_orch.task_assessment import TaskProfile, apply_confidence_policy
from lumi_orch.upgrade_policy import (
    ContextFitStatus,
    UpgradeReason,
    next_complexity,
    plan_context_fit,
)


def _profile(**overrides) -> TaskProfile:
    base = {
        "complexity": "M0",
        "confidence": 0.9,
        "intent_type": "GENERATE_ONLY",
        "side_effects": [],
        "info_sources": ["USER_PROVIDED"],
        "output_target": "CHAT",
        "execution_target": "NONE",
        "required_capabilities": [],
        "path_determinism": "KNOWN",
        "risk_level": "READ_ONLY",
        "data_sensitivity": "NORMAL",
        "context_size_estimate": "SMALL",
    }
    base.update(overrides)
    return TaskProfile(**base)


def test_scenario_1_plain_text_direct_chat():
    profile = _profile()
    decision = route(profile)
    assert decision.ok and decision.mode == ExecutionMode.DIRECT_CHAT


def test_scenario_2_workspace_read_atomic():
    profile = _profile(
        complexity="M1",
        info_sources=["WORKSPACE"],
        required_capabilities=["DOCUMENT_READ"],
    )
    decision = route(profile, workspace_bound=True)
    assert decision.mode == ExecutionMode.M1_ATOMIC_READ
    # 未绑定工作区 → 依赖拦截
    blocked = route(profile, workspace_bound=False)
    assert blocked.blocked and blocked.reason_code == "DEPENDENCY_MISSING_WORKSPACE"


def test_scenario_3_sandbox_exec_action_and_safety():
    profile = _profile(
        complexity="M1",
        intent_type="EXECUTE_ACTION",
        side_effects=["EXECUTE"],
        execution_target="SANDBOX",
        output_target="CHAT",
        risk_level="REVERSIBLE",
        required_capabilities=["CODE_EXECUTION"],
    )
    decision = route(profile)
    assert decision.mode == ExecutionMode.M1_ATOMIC_ACTION
    action = SafetyGuard.check(
        {"function": {"name": "sandbox_run_code", "arguments": {"code": "print(1)"}}},
        task_action=task_level_action(profile),
        env=ExecutionEnv.SANDBOX,
    )
    assert action == SafetyAction.ALLOW_SANDBOX_ONLY


def test_scenario_4_desktop_write_requires_approval():
    profile = _profile(
        complexity="M1",
        intent_type="EXECUTE_ACTION",
        side_effects=["WRITE"],
        execution_target="DESKTOP",
        output_target="WORKSPACE",
        risk_level="REVERSIBLE",
        required_capabilities=["WORKSPACE_MANIPULATION"],
    )
    decision = route(profile, workspace_bound=True)
    assert decision.mode == ExecutionMode.M1_ATOMIC_ACTION
    assert task_level_action(profile) == SafetyAction.REQUIRE_USER_APPROVAL
    action = SafetyGuard.check(
        {"function": {"name": "write_file", "arguments": {"path": "config.json"}}},
        task_action=task_level_action(profile),
        env=ExecutionEnv.DESKTOP,
    )
    assert action == SafetyAction.REQUIRE_USER_APPROVAL


def test_side_effects_never_route_to_direct_or_atomic_read():
    for side in (["WRITE"], ["SEND"], ["EXECUTE"], ["DELETE"], ["PUBLISH"]):
        profile = _profile(
            complexity="M1",
            side_effects=side,
            info_sources=["USER_PROVIDED"],
            execution_target="BACKEND",
        )
        decision = route(profile)
        assert decision.mode not in {ExecutionMode.DIRECT_CHAT, ExecutionMode.M1_ATOMIC_READ}
    # M0 画像但带副作用 → 必须被修正
    forced = route(_profile(side_effects=["WRITE"], complexity="M0"))
    assert forced.mode not in {ExecutionMode.DIRECT_CHAT, ExecutionMode.M1_ATOMIC_READ}


def test_security_and_service_dependency_blocks():
    assert route(_profile(), security_violated=True).reason_code == "SECURITY_VIOLATION"
    private = _profile(
        complexity="M1",
        info_sources=["PRIVATE_SERVICE"],
        required_capabilities=["EMAIL_SEND"],
    )
    assert route(private, service_authorized=False).reason_code == "DEPENDENCY_MISSING_SERVICE"


def test_safety_matrix_tool_level():
    # 沙箱内 rm -rf /：仅沙箱放行
    assert tool_level_action(
        tool="run_shell", args={"command": "rm -rf /"}, env=ExecutionEnv.SANDBOX,
    ) == SafetyAction.ALLOW_SANDBOX_ONLY
    # 真机 rm -rf /：直接阻断
    assert tool_level_action(
        tool="run_shell", args={"command": "rm -rf /"}, env=ExecutionEnv.DESKTOP,
    ) == SafetyAction.BLOCK
    # 邮件发送：必须用户确认
    assert tool_level_action(
        tool="send_email", args={"to": "boss@example.com"}, env=ExecutionEnv.BACKEND,
    ) == SafetyAction.REQUIRE_USER_APPROVAL
    # 永久删除（非回收站）：真机阻断、后端需管理员授权
    assert tool_level_action(
        tool="delete_file", args={}, env=ExecutionEnv.DESKTOP,
    ) == SafetyAction.BLOCK
    assert tool_level_action(
        tool="delete_file", args={}, env=ExecutionEnv.BACKEND,
    ) == SafetyAction.REQUIRE_ADMIN_APPROVAL
    # 回收站删除：可逆放行
    assert tool_level_action(
        tool="delete_file", args={"use_trash": True}, env=ExecutionEnv.DESKTOP,
    ) == SafetyAction.ALLOW
    # 后端不允许跑 shell
    assert tool_level_action(tool="run_shell", args={}, env=ExecutionEnv.BACKEND) == SafetyAction.BLOCK
    with pytest.raises(SafetyException):
        SafetyGuard.enforce(
            {"function": {"name": "run_shell", "arguments": {"command": "rm -rf /"}}},
            env=ExecutionEnv.DESKTOP,
        )


def test_upgrade_decision_tree_and_context_policy():
    assert next_complexity(UpgradeReason.DEPENDENCY_REQUIRED, "M1") == "M2"
    assert next_complexity(UpgradeReason.PATH_UNKNOWN, "M1") == "M3"
    assert next_complexity(UpgradeReason.MULTI_STEP_REQUIRED, "M1") == "M2"
    # 明确约束：CONTEXT_TOO_LARGE 不升级
    assert next_complexity(UpgradeReason.CONTEXT_TOO_LARGE, "M1") is None
    assert next_complexity("CONTEXT_TOO_LARGE", "M0") is None

    ok = plan_context_fit(text_length=100, size_limit=1000)
    assert ok.status == ContextFitStatus.OK
    sliced = plan_context_fit(text_length=5000, size_limit=1000)
    assert sliced.status == ContextFitStatus.SLICED and sliced.keep_chars == 1000
    multi = plan_context_fit(text_length=5000, size_limit=1000, requires_cross_segment=True)
    assert multi.status == ContextFitStatus.MULTI_STEP_REQUIRED
    # 任何情况都不得出现 CONTEXT_TOO_LARGE 状态
    assert all(
        plan.status != "CONTEXT_TOO_LARGE"
        for plan in (ok, sliced, multi)
    )


def test_low_confidence_conservative_degrade():
    risky = _profile(
        complexity="M1",
        confidence=0.3,
        side_effects=["WRITE"],
        execution_target="DESKTOP",
        risk_level="REVERSIBLE",
        path_determinism="KNOWN",
    )
    degraded = apply_confidence_policy(risky)
    assert degraded.complexity in {"M2", "M3"}
    assert degraded.path_determinism == "UNKNOWN"
    assert degraded.risk_level in {"REQUIRES_APPROVAL", "HIGH_RISK"}
    # 只读/纯生成的低置信度：不抬复杂度（否则只读问答会被误升级为编排）
    readonly = _profile(complexity="M0", confidence=0.2)
    kept = apply_confidence_policy(readonly)
    assert kept.complexity == "M0"
    # 高置信度不降级
    assert apply_confidence_policy(_profile(confidence=0.95)).complexity == "M0"
