"""执行模式判定/审批模式映射 与 统一内容提取能力声明的回归测试。"""

from __future__ import annotations

from lumi_orch import execution_mode as em
from app.workspace import context as wc


def test_approval_mode_to_execution_mode_mapping():
    # 关闭“帮我确认” → step_confirm
    assert em.resolve_execution_mode(preference="use_workspace_policy",
                                     approval_mode=wc.APPROVAL_MODE_CONFIRM) == "step_confirm"
    # 开启 → auto_routine
    assert em.resolve_execution_mode(preference="use_workspace_policy",
                                     approval_mode=wc.APPROVAL_MODE_AUTO) == "auto_routine"
    # 兼容旧快照名
    assert em.resolve_execution_mode(approval_mode="confirm_on_write") == "step_confirm"


def test_preference_overrides_policy():
    assert em.resolve_execution_mode(preference="step_confirm",
                                     approval_mode=wc.APPROVAL_MODE_AUTO) == "step_confirm"
    assert em.resolve_execution_mode(preference="auto_routine",
                                     approval_mode=wc.APPROVAL_MODE_CONFIRM) == "auto_routine"
    assert em.resolve_execution_mode(preference="direct") == "direct"


def test_execution_required_profile_cases():
    assert em.execution_required({"goal": "EXECUTE", "complexity": "ATOMIC",
                                  "safety_level": "READ_ONLY", "required_sources": ["SYSTEM_STATE"]})
    assert em.execution_required({"goal": "ANALYZE", "complexity": "SEQUENTIAL",
                                  "safety_level": "READ_ONLY", "required_sources": ["ATTACHED_FILE"]})
    assert em.execution_required({"goal": "GENERATE", "complexity": "ATOMIC",
                                  "safety_level": "RISKY_WRITE", "required_sources": ["USER_INPUT"]})
    # 轻量读取型
    assert not em.execution_required(None, workspace_available=False, has_attachments=False)
    assert not em.execution_required({"goal": "ANALYZE", "complexity": "ATOMIC",
                                      "safety_level": "READ_ONLY", "required_sources": ["USER_INPUT"]},
                                     workspace_available=False, has_attachments=False)


def test_canonical_states_and_existing_mapping():
    assert em.canonical_state("waiting_run") == "waiting_run"
    assert em.canonical_state("bogus") is None
    assert em.existing_status_for_state("planning") == "pending"
    assert em.existing_status_for_state("running_step") == "running"
    assert em.existing_status_for_state("waiting_approval") == "waiting_approval"
    assert em.existing_status_for_state("completed") == "completed"
    assert em.existing_status_for_state("cancelled") == "cancelled"


def test_content_extract_is_declared_in_read_domain_and_auto_tier():
    assert "workspace_content_extract" in wc.WORKSPACE_READ_CAPABILITIES
    from app.agents.skills.approval_policy import classify_tool_risk

    tier, risk, _reason = classify_tool_risk("mcp__lumi_pc__workspace_content_extract",
                                             {"path": "演示.pptx"})
    assert tier == "auto"
    assert risk == "low"


def test_sse_event_contract_names():
    required = {
        "plan_delta", "plan_ready", "done", "step_started", "process",
        "tool_started", "tool_completed", "step_completed", "waiting_next",
    }
    assert required <= em.SSE_EVENTS
    assert em.SSE_EVENT_PLAN_READY == "plan_ready"
    assert em.SSE_EVENT_WAITING_NEXT == "waiting_next"


def test_initial_execution_state():
    assert em.initial_execution_state("step_confirm", plan_first_enabled=True) == "waiting_run"
    assert em.initial_execution_state("step_confirm", plan_first_enabled=False) == "planning"
    assert em.initial_execution_state("auto_routine", plan_first_enabled=True) == "planning"


def test_plan_first_eligible_priority_rules():
    # 非办公 / 轻量直答 → 永不计划优先
    assert not em.plan_first_eligible(scene="chat", execution_preference="step_confirm")
    assert not em.plan_first_eligible(scene="office", requires_orchestration=False,
                                      execution_preference="step_confirm")
    # 显式 step_confirm → 恒启用（无需工作区授权快照）
    assert em.plan_first_eligible(scene="office", execution_preference="step_confirm")
    assert em.plan_first_eligible(scene="office", execution_preference="step_confirm",
                                  plan_first_global=False, has_workspace_grant=False)
    # 显式 auto_routine / direct → 不启用
    assert not em.plan_first_eligible(scene="office", execution_preference="auto_routine")
    assert not em.plan_first_eligible(scene="office", execution_preference="direct")
    # 未显式指定：仅全局开启 + 有授权快照 + manual_commit 才启用
    assert not em.plan_first_eligible(scene="office", execution_preference="use_workspace_policy")
    assert not em.plan_first_eligible(scene="office", execution_preference="use_workspace_policy",
                                      plan_first_global=True, has_workspace_grant=False)
    assert not em.plan_first_eligible(scene="office", execution_preference="use_workspace_policy",
                                      plan_first_global=True, has_workspace_grant=True,
                                      approval_mode=wc.APPROVAL_MODE_AUTO)
    assert em.plan_first_eligible(scene="office", execution_preference="use_workspace_policy",
                                  plan_first_global=True, has_workspace_grant=True,
                                  approval_mode=wc.APPROVAL_MODE_CONFIRM)
    # 未知偏好回退到 use_workspace_policy 语义
    assert not em.plan_first_eligible(scene="office", execution_preference="unknown",
                                      plan_first_global=True, has_workspace_grant=True,
                                      approval_mode=wc.APPROVAL_MODE_AUTO)
    assert em.plan_first_eligible(scene="office", execution_preference="",
                                  plan_first_global=True, has_workspace_grant=True,
                                  approval_mode=wc.APPROVAL_MODE_CONFIRM)
