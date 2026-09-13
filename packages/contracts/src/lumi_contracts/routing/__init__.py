"""路由契约（TaskProfile / RouteDecision / ExecutionRequest）。

三者物理上各自独立成模块，这里统一再导出——既有导入路径
``from lumi_contracts.routing import RouteDecision`` 与
``from lumi_contracts.routing.task_profile import TaskProfile`` 都继续可用。
"""

from __future__ import annotations

from lumi_contracts.routing.capability_preflight import (
    CLARIFICATION_OPTIONS,
    PERMANENT_PREFLIGHT_STATES,
    PREFLIGHT_ERROR_CODES,
    PREFLIGHT_NEXT_ACTIONS,
    PREFLIGHT_STATE_ALIASES,
    PREFLIGHT_STATE_BY_ERROR,
    CapabilityPreflight,
    PreflightState,
    normalize_preflight_state,
)
from lumi_contracts.routing.execution_request import ExecutionRequest
from lumi_contracts.routing.model_capability import (
    DegradationAction,
    DegradationDecision,
    ModelCapabilityProfile,
    ModalityRequest,
    decide_degradation,
    from_role_capabilities,
)
from lumi_contracts.routing.route_decision import (
    EXECUTION_MODE_VALUES,
    LEGACY_CONTRACT_MODE_TO_ROUTE_MODE,
    RouteDecision,
    RouteMode,
    RouteModeV2,
)
from lumi_contracts.routing.shadow import (
    CRITICAL_DISCREPANCIES,
    DiscrepancyType,
    RouteSource,
    ShadowRecord,
    ShadowReport,
    build_shadow_report,
    classify_discrepancy,
    discrepancy_should_use_profile,
    is_critical_discrepancy,
)
from lumi_contracts.routing.task_profile import (
    ActionIntent,
    Complexity,
    ConfidenceSource,
    ExecutionTarget,
    InfoSource,
    IntentType,
    TargetClarity,
    TargetScope,
    TaskProfile,
)

__all__ = [
    "CLARIFICATION_OPTIONS",
    "CRITICAL_DISCREPANCIES",
    "EXECUTION_MODE_VALUES",
    "LEGACY_CONTRACT_MODE_TO_ROUTE_MODE",
    "PERMANENT_PREFLIGHT_STATES",
    "PREFLIGHT_ERROR_CODES",
    "PREFLIGHT_NEXT_ACTIONS",
    "PREFLIGHT_STATE_ALIASES",
    "PREFLIGHT_STATE_BY_ERROR",
    "ActionIntent",
    "CapabilityPreflight",
    "Complexity",
    "ConfidenceSource",
    "DegradationAction",
    "DegradationDecision",
    "DiscrepancyType",
    "ExecutionRequest",
    "ExecutionTarget",
    "InfoSource",
    "IntentType",
    "ModelCapabilityProfile",
    "ModalityRequest",
    "PreflightState",
    "RouteDecision",
    "RouteMode",
    "RouteModeV2",
    "RouteSource",
    "ShadowRecord",
    "ShadowReport",
    "TargetClarity",
    "TargetScope",
    "TaskProfile",
    "build_shadow_report",
    "classify_discrepancy",
    "decide_degradation",
    "discrepancy_should_use_profile",
    "from_role_capabilities",
    "is_critical_discrepancy",
    "normalize_preflight_state",
]
