"""升级建议适配回归：失败码 → 升级决策树（含 CONTEXT_TOO_LARGE 不升级）。"""

from __future__ import annotations

from app.services.upgrade_adapter import reason_for_error, suggest_upgrade


def test_reason_mapping():
    assert reason_for_error("RESULT_REF_EXPIRED").value == "DEPENDENCY_REQUIRED"
    assert reason_for_error("STEP_NOT_FOUND").value == "DEPENDENCY_REQUIRED"
    assert reason_for_error("PATH_UNKNOWN").value == "PATH_UNKNOWN"
    assert reason_for_error("MULTI_STEP_REQUIRED").value == "MULTI_STEP_REQUIRED"
    assert reason_for_error("CONTEXT_TOO_LARGE").value == "CONTEXT_TOO_LARGE"
    assert reason_for_error("SOMETHING_ELSE") is None


def test_suggest_upgrade_targets():
    dependency = suggest_upgrade("DEPENDENCY_FAILED", current="M1")
    assert dependency["target"] == "M2"
    unknown = suggest_upgrade("STEP_EXECUTION_ERROR", current="M1")
    assert unknown["target"] == "M3"
    multi = suggest_upgrade("MULTI_STEP_REQUIRED", current="M1")
    assert multi["target"] == "M2"
    # 已高于目标时不降级
    assert suggest_upgrade("DEPENDENCY_FAILED", current="M3")["target"] == "M3"
    assert suggest_upgrade("UNKNOWN_CODE") is None


def test_context_too_large_never_upgrades():
    suggestion = suggest_upgrade("CONTEXT_TOO_LARGE", current="M1")
    assert suggestion is not None
    assert suggestion["target"] is None
    assert "不触发复杂度升级" in suggestion["note"]
