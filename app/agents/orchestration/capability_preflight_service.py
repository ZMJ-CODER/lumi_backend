"""CapabilityPreflightService：Orchestrator 的**唯一预检入口**（方案 §3.3 职责收口）。

职责划分（用户已拍板）：

* ``task_preflight``：保留为兼容入口，内部转调本服务；
* ``broker._preflight_failure``：只负责给出**底层失败原因**（能力/租约/健康探测结果），
  不直接决定用户可见结果；
* **本服务**：把底层原因收敛成对外冻结的四个状态 + UnifiedError + 工具窗口；
* ``Orchestrator``：按结果决定继续 / 请求审批 / 阻断（禁止拿空工具列表继续）。

灰度：``CAPABILITY_PREFLIGHT_V2`` 关闭时 ``enabled()`` 返回 False，调用方走旧路径；
本模块不做任何 I/O，底层探测由调用方以 ``probe`` 回调注入，因此可离线穷举测试。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.agents.orchestration.capability_preflight import (
    CapabilityPreflightResult,
    PreflightStatus,
    preflight_capabilities,
)

FLAG = "CAPABILITY_PREFLIGHT_V2"

#: 底层探测结果 → 对外冻结状态（Broker 只报事实，映射在这里）。
LOW_LEVEL_TO_STATUS: dict[str, str] = {
    "capability_unavailable": PreflightStatus.CAPABILITY_UNAVAILABLE.value,
    "provider_unhealthy": PreflightStatus.PROVIDER_UNHEALTHY.value,
    "permission_denied": PreflightStatus.PERMISSION_DENIED.value,
    "tool_not_registered": PreflightStatus.TOOL_NOT_REGISTERED.value,
    "workspace_missing": PreflightStatus.DEPENDENCY_MISSING.value,
    "approval_required": PreflightStatus.APPROVAL_REQUIRED.value,
}


class BrokerProbe(Protocol):
    """底层探测回调：只回答事实，不做用户可见判定。"""

    def __call__(self, capabilities: Sequence[str]) -> Mapping[str, str]:
        """返回 ``{capability: 底层原因}``（无问题的能力可以不出现在结果里）。"""


class CapabilityPreflightService:
    """唯一预检入口（``enabled()`` 为假时调用方保持旧路径）。"""

    def __init__(self, *, settings: Any = None) -> None:
        self._settings = settings

    def enabled(self) -> bool:
        from app.core.feature_flags import feature_enabled

        return feature_enabled(FLAG, settings=self._settings)

    def preflight(
        self,
        *,
        profile: Any = None,
        probe: BrokerProbe | None = None,
        workspace_bound: bool = True,
        requires_workspace: bool = False,
        permissions: Mapping[str, bool] | None = None,
        required_permission: str = "",
        registered_tools: Sequence[str] | None = None,
        desired_tools: Sequence[str] | None = None,
        approval_required: bool = False,
    ) -> CapabilityPreflightResult:
        """按底层探测结果做六项检查（顺序与冻结状态见 capability_preflight）。"""
        capabilities = list(
            (profile.get("required_capabilities") if isinstance(profile, Mapping) else
             getattr(profile, "required_capabilities", [])) or []
        )
        health: dict[str, str] = {}
        available: dict[str, bool] = {}
        if probe is not None and capabilities:
            for capability, reason in dict(probe(capabilities)).items():
                status = LOW_LEVEL_TO_STATUS.get(str(reason), "")
                if status in {"", PreflightStatus.PROVIDER_UNHEALTHY.value}:
                    available[str(capability)] = True
                    if status:
                        health[str(capability)] = "unhealthy"
                else:
                    available[str(capability)] = False
        return preflight_capabilities(
            profile=profile,
            workspace_bound=workspace_bound,
            requires_workspace=requires_workspace,
            required_capabilities=capabilities or None,
            available_capabilities=available or None,
            provider_health=health or None,
            permissions=permissions,
            required_permission=required_permission,
            registered_tools=registered_tools,
            desired_tools=desired_tools,
            approval_required=approval_required,
        )


#: 预检结论在 Job 快照/事件里的字段名（前端按此读取；缺失即隐藏）。
PREFLIGHT_SNAPSHOT_KEY = "preflight"
#: 预检失败时的过程事件：放在 routing 里的键 + 稳定去重键（SSE 帧与刷新投影同一行）。
PREFLIGHT_NOTICE_KEY = "preflight_notice"
PREFLIGHT_NOTICE_ENTRY_ID = "process:preflight"


def preflight_snapshot(result: CapabilityPreflightResult) -> dict[str, Any]:
    """预检结论 → 可落 Job 快照 / 可下发前端的**结构化状态**。

    只给结论与原因码，不含内部探测细节；前端据此显示"缺什么能力 / 为什么被阻断 / 等谁确认"，
    不需要自己判断能力是否可用。
    """
    return {
        "status": result.status,
        "ok": bool(result.ok),
        "error_code": result.error_code,
        "safe_message": result.error.safe_message if result.error else "",
        "retryable": bool(result.error.retryable) if result.error else False,
        "next_action": result.error.suggested_action if result.error else "",
        "question": result.question,
        "tool_window": list(result.tool_window),
        "must_call_model": bool(result.must_call_model),
        "checks": [{"name": item.name, "ok": item.ok} for item in result.checks],
    }


def attach_preflight(routing: Mapping[str, Any] | None, result: CapabilityPreflightResult) -> dict[str, Any]:
    """把预检结论并入 ``job.routing``（写入路径唯一入口；返回新字典，不改原对象）。"""
    merged = dict(routing or {})
    merged[PREFLIGHT_SNAPSHOT_KEY] = preflight_snapshot(result)
    return merged


def preflight_process_notice(result: CapabilityPreflightResult) -> dict[str, Any] | None:
    """预检失败时的 process 事件载荷（正文类事件不带内部字段）。"""
    if result.ok:
        return None
    return {
        "kind": "thinking",
        "title": "能力预检",
        "summary": (result.error.safe_message if result.error else "") or "当前任务无法继续",
        "status": "failed",
        "detail": result.error_code,
    }



__all__ = [
    "FLAG",
    "LOW_LEVEL_TO_STATUS",
    "PREFLIGHT_NOTICE_ENTRY_ID",
    "PREFLIGHT_NOTICE_KEY",
    "PREFLIGHT_SNAPSHOT_KEY",
    "BrokerProbe",
    "CapabilityPreflightService",
    "attach_preflight",
    "preflight_process_notice",
    "preflight_snapshot",
]

