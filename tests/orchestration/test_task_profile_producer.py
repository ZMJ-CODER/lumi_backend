"""canonical TaskProfile 生产（阶段 2 remainder，灰度 ``TASK_PROFILE_CANONICAL``）回归。

覆盖：

  (a) 开关关闭 → ``task_assessor`` 输出与今天逐字一致（旧视图只做别名归一）；
  (b) 开关打开 → canonical 画像携带**有依据**的动作意图/抽象能力/目标范围，并真的
      进入 ``CapabilityPreflightService``（工具窗口非空 / 缺能力阻断）；
  (c) 影子模式 → 只记录 old→new ``fingerprint()`` 差异，继续用旧结果；
  (d) 预检失败的 process 事件（canonical 开关下才产出，既有帧不变，刷新可恢复）；
  (e) 未知/歧义输入**绝不**凭空造出动作意图，算不出的字段留痕在 ``debug``。
"""

from __future__ import annotations

import asyncio
import json
import types

from loguru import logger

from app.agents.orchestration.preflight.capability_preflight import PreflightStatus
from app.agents.orchestration.preflight.capability_preflight_service import (
    FLAG as PREFLIGHT_FLAG,
)
from app.agents.orchestration.preflight.capability_preflight_service import (
    PREFLIGHT_NOTICE_ENTRY_ID,
    PREFLIGHT_NOTICE_KEY,
    PREFLIGHT_SNAPSHOT_KEY,
    CapabilityPreflightService,
    preflight_process_notice,
    preflight_snapshot,
)
from app.agents.orchestration.planning.office_plan_selection_service import (
    OfficePlanSelectionService,
    preflight_process_frame,
)
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.tca import ComplexityLevel
from app.agents.orchestration.preflight.task_preflight import capability_preflight_facts
from app.contracts.process_log import merge_job_process_log
from app.core.config import settings
from app.services.task_assessor import (
    CANONICAL_FLAG,
    AssessmentContext,
    assess_canonical_task_profile,
    assess_task_profile,
    canonical_profile,
    heuristic_profile,
    to_canonical_profile,
)
from lumi_contracts.events.process import ProcessStatus
from lumi_contracts.routing.task_profile import (
    ActionIntent,
    ConfidenceSource,
    InfoSource,
    IntentType,
    TargetScope,
    TaskProfile as CanonicalTaskProfile,
)
from lumi_orch.task_assessment import apply_confidence_policy

SHADOW_FLAG = "INTEGRATION_SHADOW_MODE"

#: 改本地文件（旧启发式：``WRITE`` 副作用 + WORKSPACE 来源；能力信号为空）。
MODIFY_REQUEST = "把 src/main.py 里的端口改成 8080"
#: 明确的编辑请求：既有副作用信号，也有 ``DOCUMENT_EDIT`` 抽象能力信号。
CAPABILITY_REQUEST = "修改工作区里的 config.yaml 配置"
#: 纯生成：无副作用、无工作区来源 → 不得造出任何动作意图。
GENERATE_REQUEST = "把这段会议记录整理成待办事项，先对齐目标再产出条目。"


class _FakeAssessor:
    """记录 TCA（预检通过后、主模型之前的那次分类回合）是否发生。"""

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

    async def plan(self, *args, **kwargs):  # pragma: no cover - 阻断/M0 路径不应到这里
        self.calls += 1
        raise AssertionError("预检失败/m0 直答路径不得调用 Planner")


def _context(request: str, *, workspace_id: str | None = None) -> PlanRequestContext:
    return PlanRequestContext.from_legacy_args(
        user_id="u1", request=request, scene="office", workspace_id=workspace_id
    )


def _service() -> OfficePlanSelectionService:
    return OfficePlanSelectionService(
        planner=_FakePlanner(), workers={}, assessor=_FakeAssessor()
    )


def _select(*, request: str, workspace_id: str | None = None, **kwargs):
    assessor = kwargs.pop("assessor", None) or _FakeAssessor()
    planner = _FakePlanner()
    service = OfficePlanSelectionService(
        planner=planner, workers={}, assessor=assessor, **kwargs
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
            planning_context=_context(request, workspace_id=workspace_id),
            routing_model={"model": "test"},
        )
    )
    return selection, assessor, planner


def _enabled_service() -> CapabilityPreflightService:
    return CapabilityPreflightService(settings=types.SimpleNamespace(**{PREFLIGHT_FLAG: True}))


def _ambiguous_notice() -> dict:
    """真实失败结论 → 过程事件载荷（不手写字段，避免与实现漂移）。"""
    result = _enabled_service().preflight(
        profile={"action_intents": ["MODIFY"], "target_clarity": "UNKNOWN"}
    )
    notice = preflight_process_notice(result)
    assert notice is not None
    return notice


# ── (a) 开关关闭：输出逐字不变 ────────────────────────────


def test_flag_off_keeps_the_legacy_profile_unchanged(monkeypatch):
    monkeypatch.setattr(settings, CANONICAL_FLAG, False)
    context = AssessmentContext(request=MODIFY_REQUEST, workspace_id="w1")

    profile, source = asyncio.run(assess_task_profile(context, use_llm=False))
    assert source == "heuristic"
    assert profile.model_dump() == apply_confidence_policy(heuristic_profile(context)).model_dump(), (
        "开关关闭时 task_assessor 必须逐字返回今天的严格画像"
    )

    canonical, canonical_source = asyncio.run(
        assess_canonical_task_profile(context, use_llm=False)
    )
    assert canonical_source == "heuristic"
    assert canonical.model_dump() == CanonicalTaskProfile.from_mapping(profile).model_dump(), (
        "开关关闭时 canonical 读法只能是别名归一的旧视图"
    )
    assert canonical.action_intents == [] and canonical.required_capabilities == []


def test_flag_off_preflight_inputs_stay_on_the_legacy_facts(monkeypatch):
    monkeypatch.setattr(settings, PREFLIGHT_FLAG, True)
    monkeypatch.setattr(settings, CANONICAL_FLAG, False)

    inputs = _service()._preflight_inputs(
        request=MODIFY_REQUEST,
        project_id=None,
        office_docs=None,
        planning_context=_context(MODIFY_REQUEST, workspace_id="w1"),
    )
    legacy = capability_preflight_facts(MODIFY_REQUEST, workspace_id="w1")
    assert inputs["profile"] == {**legacy["profile"], "required_capabilities": []}, (
        "开关关闭时预检输入必须与今天的 capability_preflight_facts 完全一致"
    )
    assert inputs["workspace_bound"] is True and inputs["requires_workspace"] is True

    selection, _, _ = _select(request=MODIFY_REQUEST, workspace_id="w1")
    assert PREFLIGHT_NOTICE_KEY not in selection.routing
    assert selection.process_notice is None


# ── (b) 开关打开：canonical 画像进入预检 ──────────────────


def test_flag_on_produces_the_canonical_profile_from_real_signals(monkeypatch):
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    profile = canonical_profile(AssessmentContext(request=MODIFY_REQUEST, workspace_id="w1"))

    assert profile.intent_type is IntentType.EXECUTE_ACTION
    assert profile.action_intents == [ActionIntent.MODIFY, ActionIntent.READ], (
        "WRITE 副作用 → MODIFY；WORKSPACE 来源（需读取本地文件）→ READ"
    )
    assert profile.target_scope is TargetScope.WORKSPACE
    assert profile.execution_target.value == "DESKTOP"
    assert profile.complexity.value == "SEQUENTIAL", "旧档位 M2 → SEQUENTIAL"
    assert profile.confidence_source is ConfidenceSource.HEURISTIC
    assert profile.decision_reason_code == "assessor.side_effects"
    assert profile.side_effects is True
    assert profile.required_capabilities == [], (
        "旧启发式没有该请求的能力信号 → 留空，不猜"
    )
    assert profile.debug["unavailable"], "算不出的字段必须留痕"
    assert "端口" not in json.dumps(profile.model_dump(mode="json")), (
        "画像是事实摘要，不含用户原文"
    )


def test_flag_on_capabilities_reach_the_preflight_as_concrete_addresses(monkeypatch):
    monkeypatch.setattr(settings, PREFLIGHT_FLAG, True)
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    seen: list[list[str]] = []

    def probe(capabilities):
        seen.append([str(item) for item in capabilities])
        return {}

    selection, assessor, planner = _select(
        request=CAPABILITY_REQUEST, workspace_id="w1", preflight_probe=probe
    )
    snapshot = selection.routing[PREFLIGHT_SNAPSHOT_KEY]
    assert snapshot["ok"] is True and snapshot["must_call_model"] is True
    assert snapshot["tool_window"] == ["workspace_navigator", "workspace_edit"], (
        "canonical 的动作意图必须点亮工具窗口（MODIFY）"
    )
    assert seen and "workspace.write@1" in seen[0], (
        "抽象能力（DOCUMENT_EDIT）要翻成 Broker 寻址键才进探测"
    )
    assert all(capability.endswith("@1") for capability in seen[0])
    assert assessor.calls == 1 and planner.calls == 0


def test_flag_on_missing_capability_blocks_before_the_model(monkeypatch):
    monkeypatch.setattr(settings, PREFLIGHT_FLAG, True)
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    selection, assessor, planner = _select(
        request=CAPABILITY_REQUEST,
        workspace_id="w1",
        preflight_probe=lambda capabilities: {
            str(item): "capability_unavailable" for item in capabilities
        },
    )
    snapshot = selection.routing[PREFLIGHT_SNAPSHOT_KEY]
    assert snapshot["status"] == PreflightStatus.CAPABILITY_UNAVAILABLE.value
    assert snapshot["tool_window"] == [] and snapshot["must_call_model"] is False
    assert assessor.calls == 0 and planner.calls == 0, "预检失败不得调用主模型"
    assert selection.tree.nodes == [] and selection.tree.clarification


# ── (c) 影子模式：只记录差异，继续用旧结果 ────────────────


def test_shadow_mode_logs_one_line_diff_and_keeps_the_old_result(monkeypatch):
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    monkeypatch.setattr(settings, SHADOW_FLAG, True)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), format="{message}", level="INFO")
    try:
        profile = canonical_profile(AssessmentContext(request=MODIFY_REQUEST, workspace_id="w1"))
    finally:
        logger.remove(sink)

    assert profile.action_intents == [] and profile.decision_reason_code == "", (
        "影子模式必须继续使用旧结果（今天是别名归一视图）"
    )
    diff = [line for line in lines if "shadow diff" in line]
    assert len(diff) == 1, "一行差异日志"
    assert "action_intents" in diff[0] and "MODIFY" in diff[0]
    assert "端口" not in diff[0], "差异日志只含枚举/原因码，不含用户原文"


def test_shadow_mode_keeps_the_old_facts_in_the_preflight(monkeypatch):
    monkeypatch.setattr(settings, PREFLIGHT_FLAG, True)
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    monkeypatch.setattr(settings, SHADOW_FLAG, True)
    inputs = _service()._preflight_inputs(
        request=CAPABILITY_REQUEST,
        project_id=None,
        office_docs=None,
        planning_context=_context(CAPABILITY_REQUEST, workspace_id="w1"),
    )
    legacy = capability_preflight_facts(CAPABILITY_REQUEST, workspace_id="w1")
    assert inputs["profile"] == {**legacy["profile"], "required_capabilities": []}, (
        "影子模式下实际事实必须与开关关闭时逐字一致（新逻辑只打点）"
    )


# ── (d) 预检失败的过程事件（客户端可见通道）───────────────


def test_preflight_process_event_only_exists_when_the_flag_is_on(monkeypatch):
    injected = lambda **_: {"action_intents": ["MODIFY"], "target_clarity": "UNKNOWN"}  # noqa: E731
    monkeypatch.setattr(settings, PREFLIGHT_FLAG, True)

    monkeypatch.setattr(settings, CANONICAL_FLAG, False)
    off, _, _ = _select(request=MODIFY_REQUEST, preflight_profile=injected)
    assert off.routing[PREFLIGHT_SNAPSHOT_KEY]["status"] == (
        PreflightStatus.NEEDS_CLARIFICATION.value
    )
    assert PREFLIGHT_NOTICE_KEY not in off.routing, "开关关闭时 routing 逐字不变"
    assert off.process_notice is None and preflight_process_frame(off.routing) is None

    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    on, _, _ = _select(request=MODIFY_REQUEST, preflight_profile=injected)
    notice = on.routing[PREFLIGHT_NOTICE_KEY]
    assert notice == _ambiguous_notice()
    assert on.process_notice == notice
    frame = preflight_process_frame(on.routing)
    assert frame is not None and frame["type"] == "process"
    assert frame["entry_id"] == PREFLIGHT_NOTICE_ENTRY_ID
    assert frame["title"] == "能力预检" and frame["status"] == "failed"
    assert frame["summary"] == notice["summary"] and frame["detail"] == "TARGET_REQUIRED"
    # 既有帧/快照形状不变：冻结字段集合原样保留。
    assert set(on.routing[PREFLIGHT_SNAPSHOT_KEY]) == set(preflight_snapshot(
        _enabled_service().preflight(
            profile={"action_intents": ["MODIFY"], "target_clarity": "UNKNOWN"}
        )
    ))


def test_preflight_notice_is_restorable_from_the_job_process_log(monkeypatch):
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    notice = _ambiguous_notice()
    job = types.SimpleNamespace(
        job_id="job-1",
        routing={"plan_text": "能力预检", PREFLIGHT_NOTICE_KEY: notice},
        nodes=[],
        process_log=[],
        created_at=0,
    )
    entries = merge_job_process_log(job)
    preflight = [entry for entry in entries if entry.entry_id == PREFLIGHT_NOTICE_ENTRY_ID]
    assert len(preflight) == 1, "刷新后必须能从 routing 恢复同一条过程"
    assert preflight[0].title == "能力预检" and preflight[0].status is ProcessStatus.FAILED
    assert preflight[0].detail == "TARGET_REQUIRED"

    monkeypatch.setattr(settings, CANONICAL_FLAG, False)
    plain = types.SimpleNamespace(
        job_id="job-1", routing={"plan_text": "能力预检"}, nodes=[], process_log=[], created_at=0
    )
    assert all(
        entry.entry_id != PREFLIGHT_NOTICE_ENTRY_ID for entry in merge_job_process_log(plain)
    )


# ── (e) 未知/歧义输入不得造出意图 ─────────────────────────


def test_unknown_input_never_fabricates_intents(monkeypatch):
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    pure = canonical_profile(AssessmentContext(request=GENERATE_REQUEST))
    assert pure.intent_type is IntentType.GENERATE_ONLY
    assert pure.action_intents == [] and pure.required_capabilities == []
    assert pure.target_scope is TargetScope.USER_INPUT
    assert pure.decision_reason_code == "assessor.generate_only"

    unknown = canonical_profile(
        AssessmentContext(request=GENERATE_REQUEST, workspace_id="w1")
    )
    assert unknown.action_intents == [ActionIntent.READ], "WORKSPACE 来源是读取事实，不是动作猜测"
    assert set(unknown.required_capabilities) <= {"WORKSPACE_MANIPULATION"}


def test_unmappable_legacy_values_are_recorded_not_guessed(monkeypatch):
    monkeypatch.setattr(settings, CANONICAL_FLAG, True)
    legacy = types.SimpleNamespace(
        complexity="M2",
        confidence=0.9,
        intent_type="EXECUTE_ACTION",
        side_effects=["TELEPORT"],
        info_sources=["EXTERNAL_WEB", "NOT_A_SOURCE"],
        output_target="CHAT",
        execution_target="BACKEND",
        required_capabilities=[],
        path_determinism="KNOWN",
        risk_level="READ_ONLY",
    )
    mapped = to_canonical_profile(legacy, source="llm")
    assert mapped.action_intents == [], "无法映射的副作用词不得变成动作意图"
    assert mapped.debug["unmapped_side_effects"] == ["TELEPORT"]
    assert mapped.debug["unmapped_info_sources"] == ["NOT_A_SOURCE"]
    assert mapped.info_sources == [InfoSource.PUBLIC_WEB], "EXTERNAL_WEB 在 canonical 里是 PUBLIC_WEB"
    assert mapped.execution_target.value == "SERVER", "BACKEND → SERVER（有损，留痕）"
    assert mapped.confidence_source is ConfidenceSource.LLM
    assert mapped.approval_required is False, "只有 HIGH_RISK 才是审批事实"

    alias_only = CanonicalTaskProfile.from_mapping(legacy)
    assert alias_only.action_intents == []
    assert alias_only.debug["coerced_fields"], "旧画像的非法枚举值要留痕（影子比对用）"
