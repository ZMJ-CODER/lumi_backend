"""动态升级建议的 app 适配：把执行失败码映射为升级决策树输入。

策略在 ``lumi_orch.upgrade_policy``：仅 DEPENDENCY_REQUIRED / PATH_UNKNOWN /
MULTI_STEP_REQUIRED 允许升级；CONTEXT_TOO_LARGE 明确不升级。
"""

from __future__ import annotations

from lumi_orch.upgrade_policy import UpgradeReason, next_complexity

# 执行/校验失败码 → 升级原因
_CODE_TO_REASON: dict[str, UpgradeReason] = {
    "RESULT_REF_EXPIRED": UpgradeReason.DEPENDENCY_REQUIRED,
    "DEPENDENCY_FAILED": UpgradeReason.DEPENDENCY_REQUIRED,
    "STEP_DEPENDENCIES_NOT_MET": UpgradeReason.DEPENDENCY_REQUIRED,
    "STEP_NOT_FOUND": UpgradeReason.DEPENDENCY_REQUIRED,
    "RESOURCE_COORDINATION_UNAVAILABLE": UpgradeReason.DEPENDENCY_REQUIRED,
    "PATH_UNKNOWN": UpgradeReason.PATH_UNKNOWN,
    "ROUTE_UNKNOWN": UpgradeReason.PATH_UNKNOWN,
    "STEP_EXECUTION_ERROR": UpgradeReason.PATH_UNKNOWN,
    "MULTI_STEP_REQUIRED": UpgradeReason.MULTI_STEP_REQUIRED,
    "CONTEXT_TOO_LARGE": UpgradeReason.CONTEXT_TOO_LARGE,
}


def reason_for_error(error_code: str) -> UpgradeReason | None:
    return _CODE_TO_REASON.get(str(error_code or "").strip().upper())


def suggest_upgrade(error_code: str, *, current: str = "M1") -> dict | None:
    """返回升级建议 dict（含 reason/target/note）；不升级时返回 None。"""
    reason = reason_for_error(error_code)
    if reason is None:
        return None
    target = next_complexity(reason, current if current in {"M0", "M1", "M2", "M3"} else "M1")
    if target is None:
        # 明确不升级（如 CONTEXT_TOO_LARGE 由 Resolver 内部处理）。
        return {
            "reason": reason.value,
            "target": None,
            "note": "该状态不触发复杂度升级（由 InformationResolver 内部截断/分段处理）。",
        }
    return {
        "reason": reason.value,
        "target": target,
        "note": "步骤失败原因满足升级条件，建议以更高复杂度重新编排。",
    }


__all__ = ["reason_for_error", "suggest_upgrade"]
