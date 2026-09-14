"""能力预检接入主链路（灰度 ``CAPABILITY_PREFLIGHT_V2``）回归。

覆盖：
  (a) 开关关闭 → ``preflight_external_effect`` 与 Orchestrator 预检路径行为不变；
  (b) 开关打开 → 预检失败不调用主模型、不给空工具窗口，``routing["preflight"]`` 可见；
  (c) 开关打开 + 通过 → happy path 不变，并带上解析出的工具窗口；
  (d) ``broker._preflight_failure`` 只返回底层原因（不再决定用户可见结果）。
"""

from __future__ import annotations

import asyncio
import types

from app.agents.orchestration.preflight.capability_preflight import (
    FROZEN_PREFLIGHT_STATES,
    CapabilityPreflightResult,
    PreflightStatus,
)
from app.agents.orchestration.preflight.capability_preflight_service import (
    FLAG,
    PREFLIGHT_SNAPSHOT_KEY,
    CapabilityPreflightService,
)
from app.agents.orchestration.planning.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.tca import ComplexityLevel
from app.agents.orchestration.preflight.task_preflight import preflight_external_effect
from app.core.config import settings

CLARIFY_REQUEST = "帮我修改一下"
SCOPE_REQUEST = "把本地文件改一下"
HAPPY_REQUEST = "把这句话改得更正式一些。"
ACTION_REQUEST = "把 src/main.py 里的端口改成 8080"
SNAPSHOT_FIELDS = {
    "status", "ok", "error_code", "safe_message", "safe_next_action", "retryable",
    "next_action", "needs_human", "permanent", "question", "tool_window",
    "required_capabilities", "must_call_model", "checks",
}


def _enabled_service() -> CapabilityPreflightService:
    """服务实例：只用于构造"事实"参照物（开关由 settings 决定）。"""
    return CapabilityPreflightService(settings=types.SimpleNamespace(**{FLAG: True}))


class _FakeAssessor:
    """记录 TCA（本调用点上"主模型调用"之前的分类回合）是否发生。"""

    def __init__(self, level: ComplexityLevel = ComplexityLevel.M0) -> None:
        self.level = level
        self.calls = 0

    async def assess(self, request, *, office_docs=None, prior_summaries=""):
        self.calls += 1
        return types.SimpleNamespace(
            level=self.level,
            mode=types.SimpleNamespace(value="plan_execute"),
            audit_dict=lambda: {"level": self.level.value, "reasons": []},
        )


class _FakePlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *args, **kwargs):  # pragma: no cover - M0 路径不应到这里
        self.calls += 1
        raise AssertionError("预检失败/m0 直答路径不得调用 Planner")


def _select(*, request: str, workspace_id: str | None = None, **kwargs):
    assessor = kwargs.pop("assessor", None) or _FakeAssessor()
    planner = _FakePlanner()
    service = OfficePlanSelectionService(
        planner=planner, workers={}, assessor=assessor, **kwargs
    )
    context = PlanRequestContext.from_legacy_args(
        user_id="u1", request=request, scene="office", workspace_id=workspace_id
    )
    selection = asyncio.run(
        service.select(
            user_id="u1",
            request=request,
            user_role="user",
            project_id=None,
            project_ids=None,
            clarification_answer=None,
            office_docs=None,
            prior_summaries="",
            planning_context=context,
            routing_model={"model": "test"},
        )
    )
    return selection, assessor, planner


# ── (a) 开关关闭：旧行为不变 ─────────────────────────────


def test_flag_off_preflight_external_effect_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, FLAG, False)
    assert preflight_external_effect("写一份项目延期说明").needs_clarification is False
    bare = preflight_external_effect(CLARIFY_REQUEST)
    assert bare.needs_clarification is True
    assert bare.reason == "effect_without_target"
    assert bare.question == "请说明希望我处理什么内容或对象，以及期望的结果。"
    scope = preflight_external_effect(SCOPE_REQUEST)
    assert scope.needs_clarification is True
    assert scope.reason == "local_effect_without_scope"
    assert scope.question == "请先选择或创建要操作的工作区，并说明目标路径及新建、修改或覆盖方式。"
    assert preflight_external_effect("修改 src/main.py").reason == "local_effect_without_scope"
    assert preflight_external_effect("修改 src/main.py", workspace_id="w1").needs_clarification is False


def test_flag_off_does_not_touch_the_preflight_service(monkeypatch):
    monkeypatch.setattr(settings, FLAG, False)

    def _boom(self, **kwargs):  # pragma: no cover - 只在被误调用时触发
        raise AssertionError("开关关闭时不得调用 CapabilityPreflightService")

    monkeypatch.setattr(CapabilityPreflightService, "preflight", _boom)
    assert preflight_external_effect(SCOPE_REQUEST).needs_clarification is True
    selection, assessor, _ = _select(request=SCOPE_REQUEST)
    assert selection.routing["fallback_action"] == "preflight_clarification"
    assert PREFLIGHT_SNAPSHOT_KEY not in selection.routing
    assert assessor.calls == 0


def test_flag_off_orchestrator_path_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, FLAG, False)
    selection, assessor, planner = _select(request=CLARIFY_REQUEST)
    latency_ms = selection.routing.pop("route_latency_ms")
    assert isinstance(latency_ms, int)
    assert selection.routing == {
        "planner_invoked": False,
        "tca_invoked": False,
        "fallback_action": "preflight_clarification",
        "preflight_reason": "effect_without_target",
        "level": ComplexityLevel.M3.value,
    }
    assert selection.tree.nodes == [] and selection.tree.clarification
    assert assessor.calls == 0 and planner.calls == 0

    passed, assessor_ok, _ = _select(request=HAPPY_REQUEST)
    assert assessor_ok.calls == 1
    assert passed.routing["fallback_action"] == "m0_direct"
    assert PREFLIGHT_SNAPSHOT_KEY not in passed.routing


# ── (b) 开关打开：失败阻断 ───────────────────────────────


def test_flag_on_workspace_failure_blocks_the_model_call(monkeypatch):
    monkeypatch.setattr(settings, FLAG, True)
    selection, assessor, planner = _select(
        request=ACTION_REQUEST,
        preflight_profile=lambda **_: {
            "action_intents": ["MODIFY"], "target_clarity": "KNOWN", "requires_workspace": True,
        },
    )
    snapshot = selection.routing[PREFLIGHT_SNAPSHOT_KEY]
    assert snapshot["status"] == PreflightStatus.DEPENDENCY_MISSING.value
    assert snapshot["status"] in FROZEN_PREFLIGHT_STATES
    assert snapshot["tool_window"] == []
    assert snapshot["must_call_model"] is False
    assert set(snapshot) == SNAPSHOT_FIELDS, "只允许 preflight_snapshot 的字段（不泄漏内部信息）"
    assert assessor.calls == 0 and planner.calls == 0, "预检失败不得调用主模型"
    assert selection.tree.nodes == [] and selection.tree.clarification


def test_flag_on_capability_failure_through_the_broker_probe(monkeypatch):
    monkeypatch.setattr(settings, FLAG, True)
    probed: list[list[str]] = []

    def probe(capabilities):
        probed.append(list(capabilities))
        return {"workspace.write": "capability_unavailable"}

    selection, assessor, _ = _select(
        request=HAPPY_REQUEST,
        preflight_profile=lambda **_: {
            "action_intents": ["MODIFY"],
            "target_clarity": "KNOWN",
            "required_capabilities": ["workspace.write"],
            "workspace_bound": True,
        },
        preflight_probe=probe,
    )
    assert probed == [["workspace.write"]], "预检必须经 probe 拿底层事实（Broker 只报事实）"
    snapshot = selection.routing[PREFLIGHT_SNAPSHOT_KEY]
    assert snapshot["status"] == PreflightStatus.CAPABILITY_UNAVAILABLE.value
    assert snapshot["status"] in FROZEN_PREFLIGHT_STATES
    assert snapshot["tool_window"] == [] and snapshot["must_call_model"] is False
    assert assessor.calls == 0


# ── (c) 开关打开：通过时 happy path 不变 ──────────────────


def test_flag_on_success_keeps_the_happy_path_with_tool_window(monkeypatch):
    monkeypatch.setattr(settings, FLAG, True)
    selection, assessor, _ = _select(
        request=HAPPY_REQUEST,
        preflight_profile=lambda **_: {"action_intents": ["MODIFY"], "target_clarity": "KNOWN"},
    )
    snapshot = selection.routing[PREFLIGHT_SNAPSHOT_KEY]
    assert snapshot["status"] == PreflightStatus.READY.value and snapshot["ok"] is True
    assert snapshot["must_call_model"] is True
    assert snapshot["tool_window"] == ["workspace_navigator", "workspace_edit"]
    # 通过时计划路径与开关关闭时一致（同一路由结论）。
    assert selection.routing["fallback_action"] == "m0_direct"
    assert selection.routing["planner_invoked"] is False
    assert assessor.calls == 1


def test_flag_on_approval_required_defers_to_the_existing_approval_flow(monkeypatch):
    monkeypatch.setattr(settings, FLAG, True)
    selection, assessor, _ = _select(
        request=HAPPY_REQUEST,
        preflight_profile=lambda **_: {
            "action_intents": ["MODIFY"], "target_clarity": "KNOWN", "approval_required": True,
        },
    )
    snapshot = selection.routing[PREFLIGHT_SNAPSHOT_KEY]
    assert snapshot["status"] == PreflightStatus.APPROVAL_REQUIRED.value
    assert selection.routing["fallback_action"] == "m0_direct"
    assert assessor.calls == 1, "审批走既有流程，不在预检处阻断"


# ── 兼容入口在开关打开时确实委派给服务 ────────────────────


def test_flag_on_preflight_external_effect_delegates_to_the_service(monkeypatch):
    monkeypatch.setattr(settings, FLAG, True)
    calls: list[dict] = []
    delegated = _enabled_service().preflight(
        profile={"action_intents": ["MODIFY"], "target_clarity": "KNOWN"},
        workspace_bound=False,
        requires_workspace=True,
    )
    assert isinstance(delegated, CapabilityPreflightResult)

    def fake_preflight(self, **kwargs):
        calls.append(kwargs)
        return delegated

    monkeypatch.setattr(CapabilityPreflightService, "preflight", fake_preflight)
    result = preflight_external_effect(SCOPE_REQUEST)
    assert calls, "开关打开时必须由 CapabilityPreflightService 判定"
    assert result.needs_clarification is True
    assert result.reason == "local_effect_without_scope", "原因语义与旧路径一致"
    assert result.question


# ── (d) Broker 只报底层事实 ──────────────────────────────


def test_broker_preflight_failure_only_reports_low_level_facts(monkeypatch):
    from lumi_contracts.plugins import CapabilityInvocation

    from app.agents.capabilities.broker.broker import (
        PREFLIGHT_FACTS,
        CapabilityBroker,
        CapabilitySelection,
    )

    broker = CapabilityBroker()
    invocation = CapabilityInvocation(capability="workspace.write", arguments={})
    selection = CapabilitySelection(descriptor=None)

    monkeypatch.setattr(settings, FLAG, False)
    legacy = broker._preflight_failure(invocation, selection)
    assert legacy is not None and legacy.error is not None
    # 旧路径（用户可见文案）保持不变
    assert "未声明的能力" in legacy.error.message
    assert "请安装或启用" in legacy.error.suggested_action

    monkeypatch.setattr(settings, FLAG, True)
    fact_only = broker._preflight_failure(invocation, selection)
    assert fact_only is not None and fact_only.error is not None
    assert fact_only.error.code == legacy.error.code, "错误码契约不变"
    assert fact_only.error.message == "capability_unavailable"
    assert fact_only.error.message in PREFLIGHT_FACTS
    assert fact_only.error.suggested_action == ""
    assert fact_only.error.details == {"reason": "capability_unavailable"}
    assert "请" not in fact_only.error.message, "不得携带用户可见文案"


def test_broker_probe_reports_facts_only():
    from app.agents.capabilities.broker.broker import PREFLIGHT_FACTS, CapabilityBroker

    facts = CapabilityBroker().preflight_facts(["lumi.not-declared@1"])
    assert facts == {"lumi.not-declared@1": "capability_unavailable"}
    assert set(facts.values()) <= PREFLIGHT_FACTS


def test_preflight_snapshot_survives_the_route_snapshot_projection():
    """路由快照投影只清自己管理的字段：``preflight`` 必须原样落到 ``job.routing``。"""
    from app.agents.orchestration.preflight.capability_preflight_service import attach_preflight
    from app.agents.orchestration.planning.route_snapshot import apply_route_snapshot

    result = _enabled_service().preflight(
        profile={"action_intents": ["MODIFY"], "target_clarity": "KNOWN"}, workspace_bound=True
    )
    routing = attach_preflight({"level": "m0", "task_profile": {"complexity": "M0"}}, result)
    expected = routing[PREFLIGHT_SNAPSHOT_KEY]
    apply_route_snapshot(routing, {"complexity": "M0", "task_profile": {"complexity": "M0"}})
    assert routing[PREFLIGHT_SNAPSHOT_KEY] == expected
