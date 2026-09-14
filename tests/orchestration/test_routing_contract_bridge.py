"""阶段三回归：编排边界的路由契约桥接。

断言两件事：

1. 契约对象是**类型化**的：模式/画像/执行请求都能从 ``RoutedTask`` 直接取到，
   下游不必再解析裸字符串；
2. 旧快照字段（``meta()``）**逐字节保持**，包括历史 ``route_mode`` 字符串——
   契约只增加视图，不改动既有输出。
"""

from __future__ import annotations

import pytest
from lumi_contracts import ExecutionRequest, RouteMode, Sensitivity, ServerContext
from lumi_orch.execution_router import ExecutionMode, RouteDecision
from lumi_orch.safety_policy import SafetyAction
from lumi_orch.task_assessment import TaskProfile

from app.contracts.routing import (
    CONTRACT_MODE_BY_LEGACY,
    legacy_route_mode,
    route_decision_contract,
    task_profile_contract,
)
from app.services.task_router_adapter import RoutedTask


def _routed(
    mode: ExecutionMode | None = ExecutionMode.M1_ATOMIC_READ,
    *,
    profile: TaskProfile | None = None,
    blocked: bool = False,
    reason: str = "",
) -> RoutedTask:
    return RoutedTask(
        profile=profile or TaskProfile(complexity="M1"),
        decision=RouteDecision(mode, blocked=blocked, reason_code="X" if blocked else "", reason=reason),
        safety_action=SafetyAction.ALLOW,
        assessor_source="test",
    )


@pytest.mark.parametrize(
    ("legacy_mode", "expected"),
    [
        (ExecutionMode.DIRECT_CHAT, RouteMode.DIRECT_CHAT),
        (ExecutionMode.M1_ATOMIC_READ, RouteMode.ATOMIC_READ),
        (ExecutionMode.M1_ATOMIC_ACTION, RouteMode.SINGLE_ACTION),
        (ExecutionMode.SEQUENTIAL_WORKFLOW, RouteMode.PLANNER_DAG),
        (ExecutionMode.DYNAMIC_AGENT, RouteMode.REACT),
    ],
)
def test_execution_mode_maps_to_contract_route_mode(legacy_mode, expected):
    routed = _routed(legacy_mode)
    assert routed.contract_decision().mode is expected
    # 反向映射必须回到同一个历史字符串（快照兼容）。
    assert legacy_route_mode(expected) == legacy_mode.value
    assert CONTRACT_MODE_BY_LEGACY[legacy_mode.value] is expected


def test_blocked_decision_keeps_reason_and_has_no_legacy_mode():
    routed = _routed(None, blocked=True, reason="请先绑定工作区")
    decision = routed.contract_decision()
    assert decision.mode is RouteMode.BLOCKED
    assert decision.blocked_reason == "请先绑定工作区"
    assert decision.signals["blocked"] is True
    assert legacy_route_mode(decision.mode) == ""


def test_task_profile_contract_keeps_unmappable_facts_in_debug():
    profile = TaskProfile(
        complexity="M2",
        side_effects=["WRITE"],
        info_sources=["WORKSPACE", "EXTERNAL_WEB"],
        execution_target="DESKTOP",
        required_capabilities=["DOCUMENT_READ"],
        path_determinism="UNKNOWN",
        estimated_steps=3,
        risk_level="REQUIRES_APPROVAL",
        confidence=0.4,
    )
    contract = task_profile_contract(profile)
    assert contract.complexity.value == "SEQUENTIAL"
    assert contract.side_effects is True
    assert contract.execution_target.value == "DESKTOP"
    assert {item.value for item in contract.info_sources} == {"WORKSPACE", "PUBLIC_WEB"}
    assert contract.required_capabilities == ["DOCUMENT_READ"]
    # 粗粒度契约放不下的事实必须留在 debug，而不是被丢掉。
    assert contract.debug["path_determinism"] == "UNKNOWN"
    assert contract.debug["estimated_steps"] == 3
    assert contract.debug["side_effects"] == ["WRITE"]
    assert contract.needs_workspace is True
    assert contract.confidence == 0.4


def test_unknown_info_source_is_reported_not_guessed():
    profile = TaskProfile(complexity="M1")
    profile.info_sources.append("SOMETHING_NEW")  # type: ignore[arg-type]
    contract = task_profile_contract(profile)
    assert contract.debug["unmapped_info_sources"] == ["SOMETHING_NEW"]
    assert "SOMETHING_NEW" not in {item.value for item in contract.info_sources}


def test_meta_snapshot_is_unchanged_by_the_contract_bridge():
    routed = _routed(ExecutionMode.SEQUENTIAL_WORKFLOW, profile=TaskProfile(complexity="M2"))
    meta = routed.meta()
    assert meta["route_mode"] == "sequential_workflow"
    assert meta["policy_version"] == "router_v2"
    assert meta["assessor_source"] == "test"
    # task_profile 仍是评估画像原始 dump（不是契约画像）。
    assert meta["task_profile"]["complexity"] == "M2"
    assert "path_determinism" in meta["task_profile"]
    assert set(meta) == {
        "task_profile",
        "route_mode",
        "route_reason_code",
        "safety_action",
        "assessor_source",
        "policy_version",
        # 方案 4：人工介入信号随快照落盘（缺省 False；预检结论只在预检后出现）。
        "approval_required",
        "needs_clarification",
    }


def test_meta_route_mode_is_empty_without_a_mode():
    assert _routed(None).meta()["route_mode"] == ""


def test_execution_request_carries_server_identity_and_contract_route():
    routed = _routed(ExecutionMode.M1_ATOMIC_READ)
    context = ServerContext(user_id="u1", workspace_id="ws-1", data_sensitivity=Sensitivity.CONFIDENTIAL)
    request = routed.execution_request(
        "读一下 README",
        context=context,
        allowed_tools=("workspace_navigator",),
        max_steps=2,
    )
    assert isinstance(request, ExecutionRequest)
    assert request.context.user_id == "u1"
    assert request.context.workspace_id == "ws-1"
    assert request.data_sensitivity is Sensitivity.CONFIDENTIAL
    assert request.route is not None and request.route.mode is RouteMode.ATOMIC_READ
    assert request.allowed_tools == ("workspace_navigator",)
    assert request.max_steps == 2


def test_route_decision_contract_accepts_profile_free_decision():
    decision = route_decision_contract(RouteDecision(ExecutionMode.DIRECT_CHAT))
    assert decision.profile is None
    assert decision.required_capabilities == []
