"""统一 TaskProfile 契约回归（阶段 1 冻结项：contracts 为唯一权威定义）。

要求：§3.1 字段齐全、旧字段保持兼容（加法变更）、兼容转换器只做别名归一不猜语义、
影子比对用的 fingerprint 稳定且不含用户原文。
"""

from __future__ import annotations

from types import SimpleNamespace

from lumi_contracts import TaskProfile
from lumi_contracts.routing.task_profile import (
    ActionIntent,
    ConfidenceSource,
    IntentType,
    TargetClarity,
    TargetScope,
)


def test_contracts_profile_has_the_frozen_section_3_1_fields():
    profile = TaskProfile()
    for field in (
        "intent_type", "action_intents", "target_scope", "target_clarity",
        "has_dependency", "has_runtime_decision", "required_capabilities",
        "risk_level", "approval_required", "confidence", "confidence_source",
        "decision_reason_code",
    ):
        assert field in TaskProfile.model_fields, field
    assert profile.intent_type is IntentType.GENERATE_ONLY
    assert profile.target_scope is TargetScope.NONE
    assert profile.target_clarity is TargetClarity.KNOWN
    assert profile.confidence_source is ConfidenceSource.HEURISTIC


def test_legacy_fields_are_unchanged_by_the_addition():
    """加法变更：旧读者（goal/complexity/info_sources/debug）行为不变。"""
    profile = TaskProfile(goal="写报告", risk_level="medium", confidence=0.8)
    assert profile.goal == "写报告" and profile.risk_level == "medium"
    assert profile.needs_workspace is False
    assert profile.model_dump()["goal"] == "写报告"


def test_from_mapping_normalises_aliases():
    profile = TaskProfile.from_mapping({
        "goal": "改文件",
        "intent": "EXECUTE_ACTION",
        "actions": ["MODIFY"],
        "scope": "WORKSPACE",
        "clarity": "UNKNOWN",
        "capabilities": ["workspace.write"],
        "confidence_source_kind": "llm",
    })
    assert profile.intent_type is IntentType.EXECUTE_ACTION
    assert profile.action_intents == [ActionIntent.MODIFY]
    assert profile.target_scope is TargetScope.WORKSPACE
    assert profile.target_clarity is TargetClarity.UNKNOWN
    assert profile.required_capabilities == ["workspace.write"]
    assert profile.confidence_source is ConfidenceSource.LLM


def test_from_mapping_accepts_objects_and_passes_through_profiles():
    legacy = SimpleNamespace(goal="读文件", intent_type="EXECUTE_ACTION", action_intents=["READ"])
    profile = TaskProfile.from_mapping(legacy)
    assert profile.action_intents == [ActionIntent.READ]
    assert TaskProfile.from_mapping(profile) is profile


def test_from_mapping_ignores_unknown_fields_and_records_coercion_failures():
    profile = TaskProfile.from_mapping({"goal": "x", "not_a_field": 1, "actions": ["TELEPORT"]})
    assert profile.action_intents == []
    assert "coerced_fields" in profile.debug, "解析不了的枚举值必须留痕（影子比对用）"
    assert "not_a_field" not in profile.debug


def test_fingerprint_is_stable_and_leaks_no_user_text():
    profile = TaskProfile.from_mapping({
        "goal": "这是用户原文不要外泄", "intent": "EXECUTE_ACTION", "actions": ["MODIFY", "READ"],
    })
    fingerprint = profile.fingerprint()
    assert fingerprint["action_intents"] == ["MODIFY", "READ"], "排序稳定"
    assert "用户原文" not in str(fingerprint)
    assert set(fingerprint) >= {"intent_type", "action_intents", "complexity", "decision_reason_code"}


def test_router_hard_constraint_is_expressible_from_the_profile():
    """§3.2 硬约束所需信息都在画像里：action_intents 非空 → 不许 DIRECT_CHAT。"""
    pure = TaskProfile(intent_type=IntentType.GENERATE_ONLY)
    acting = TaskProfile(intent_type=IntentType.EXECUTE_ACTION, action_intents=[ActionIntent.CREATE])
    assert not pure.action_intents
    assert acting.action_intents and acting.intent_type is IntentType.EXECUTE_ACTION
    assert "ActionIntent" in str(TaskProfile.model_fields["action_intents"].annotation)
