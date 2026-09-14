"""CapabilityPreflightService 收口回归（阶段 2 第一步：Broker 只报事实）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.orchestration.preflight.capability_preflight import PreflightStatus
from app.agents.orchestration.preflight.capability_preflight_service import (
    FLAG,
    LOW_LEVEL_TO_STATUS,
    CapabilityPreflightService,
)

PROFILE = {"intent_type": "EXECUTE_ACTION", "action_intents": ["MODIFY"],
           "required_capabilities": ["workspace.write"], "target_clarity": "KNOWN"}
TOOLS = {"workspace_navigator", "workspace_edit"}


def _service(enabled: bool) -> CapabilityPreflightService:
    return CapabilityPreflightService(settings=SimpleNamespace(**{FLAG: enabled}))


def test_service_is_gated_by_the_feature_flag():
    assert _service(False).enabled() is False, "开关关掉时必须走旧路径"
    assert _service(True).enabled() is True


def test_broker_reason_is_translated_not_decided_by_broker():
    """Broker 只回事实（原因字符串），对外状态由本服务映射。"""
    result = _service(True).preflight(
        profile=PROFILE, probe=lambda caps: {"workspace.write": "capability_unavailable"},
        registered_tools=TOOLS,
    )
    assert result.status == PreflightStatus.CAPABILITY_UNAVAILABLE.value
    assert result.error is not None and result.error.safe_message
    assert result.must_call_model is False and result.tool_window == (), "禁止空工具列表继续"


def test_provider_unhealthy_keeps_capability_available_but_blocks_with_retryable():
    result = _service(True).preflight(
        profile=PROFILE, probe=lambda caps: {"workspace.write": "provider_unhealthy"},
        registered_tools=TOOLS,
    )
    assert result.status == PreflightStatus.PROVIDER_UNHEALTHY.value
    assert result.error is not None and result.error.retryable is True


def test_clean_probe_passes_and_returns_the_tool_window():
    result = _service(True).preflight(
        profile=PROFILE, probe=lambda caps: {}, registered_tools=TOOLS,
    )
    assert result.ok and result.must_call_model is True
    assert result.tool_window == ("workspace_navigator", "workspace_edit")


def test_every_broker_reason_maps_to_a_frozen_status():
    from app.agents.orchestration.preflight.capability_preflight import FROZEN_PREFLIGHT_STATES

    for reason, status in LOW_LEVEL_TO_STATUS.items():
        assert status == PreflightStatus.PROVIDER_UNHEALTHY.value or status in FROZEN_PREFLIGHT_STATES | {
            PreflightStatus.TOOL_NOT_REGISTERED.value
        }, reason

def test_preflight_snapshot_is_a_structured_state_for_the_frontend():
    from app.agents.orchestration.preflight.capability_preflight_service import (
        PREFLIGHT_SNAPSHOT_KEY, attach_preflight, preflight_process_notice, preflight_snapshot,
    )

    failed = _service(True).preflight(
        profile=PROFILE, probe=lambda caps: {"workspace.write": "capability_unavailable"},
        registered_tools=TOOLS,
    )
    snap = preflight_snapshot(failed)
    assert snap["status"] == "CAPABILITY_UNAVAILABLE" and snap["ok"] is False
    assert snap["error_code"] == "CAPABILITY_UNAVAILABLE" and snap["safe_message"]
    assert snap["must_call_model"] is False and snap["tool_window"] == []
    assert set(snap) >= {"status", "ok", "error_code", "safe_message", "retryable", "next_action", "checks"}

    routing = attach_preflight({"complexity": "M1"}, failed)
    assert routing["complexity"] == "M1", "原有路由字段不受影响"
    assert routing[PREFLIGHT_SNAPSHOT_KEY]["status"] == "CAPABILITY_UNAVAILABLE"
    assert "preflight" not in {"complexity": "M1"}, "attach 返回新字典，不改原对象"

    notice = preflight_process_notice(failed)
    assert notice["status"] == "failed" and notice["summary"] == snap["safe_message"]
    ok = _service(True).preflight(profile=PROFILE, probe=lambda caps: {}, registered_tools=TOOLS)
    assert preflight_process_notice(ok) is None, "通过时不发过程事件"
