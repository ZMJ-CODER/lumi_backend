"""任务理解 / 路由 / 能力预检回归（方案 4 §1–§5、§8.2）。

覆盖方案的硬约束与关键场景：

* **Router 硬约束**：``action_intents`` 非空禁直聊/原子读；"旧词表判只读、新画像判写入"
  必须以新画像为准；DELETE/EXECUTE/PUBLISH 强制审批或阻断；目标未知 → 澄清不猜路径；
* **TaskProfile**：有效动作意图推导、审批判定、契约投影带上 §1.1 字段；
* **route_snapshot v2**：新字段 + ``capability_preflight`` 位 + 不写空对象；
* **能力预检**：九态（含 ``SECURITY_BLOCKED``）、插件启用联动、``safe_next_action``、
  失败不调模型/不给空工具集；
* **影子模式**：旧读新写分类、记录只含枚举、不改变实际路由、报告聚合；
* **契约对齐**：内核词表与契约词表逐值一致（防止两侧漂移）。
"""

from __future__ import annotations

import asyncio

import pytest

from lumi_contracts import (
    EXECUTION_MODE_VALUES,
    ActionIntent,
    CapabilityPreflight,
    DiscrepancyType,
    PreflightState,
    TargetClarity,
    TargetScope,
    build_shadow_report,
    classify_discrepancy,
    discrepancy_should_use_profile,
)
from lumi_orch.execution_router import ExecutionMode, route
from lumi_orch.safety_policy import SafetyAction, task_level_action
from lumi_orch.task_assessment import (
    HIGH_RISK_SIDE_EFFECTS,
    TaskProfile,
    effective_action_intents,
    requires_approval,
)
from app.agents.orchestration.preflight.capability_preflight import (
    PERMANENT_PREFLIGHT_STATES,
    STATUS_ERROR_CODES,
    STATUS_NEXT_ACTIONS,
    preflight_capabilities,
)
from app.agents.orchestration.preflight.capability_preflight_service import (
    CapabilityPreflightService,
    plugin_capability_availability,
    preflight_snapshot,
)
from app.agents.orchestration.planning.route_snapshot import (
    ROUTE_DECISION_SCHEMA_VERSION,
    ROUTER_V2_POLICY_VERSION,
    build_route_snapshot,
)
from app.contracts.routing import route_decision_contract, task_profile_contract
from app.services import task_shadow


def _profile(**overrides) -> TaskProfile:
    base = dict(complexity="M1", confidence=0.9, path_determinism="KNOWN")
    base.update(overrides)
    return TaskProfile(**base)


# ── 契约对齐（两侧词表必须逐值一致）────────────────────────


def test_kernel_and_contract_vocabularies_match():
    assert set(EXECUTION_MODE_VALUES) == {item.value for item in ExecutionMode}
    assert {item.value for item in PreflightState} >= {
        "READY", "DEPENDENCY_MISSING_WORKSPACE", "CAPABILITY_UNAVAILABLE", "PROVIDER_UNHEALTHY",
        "PERMISSION_DENIED", "TOOL_NOT_REGISTERED", "NEEDS_CLARIFICATION",
        "APPROVAL_REQUIRED", "SECURITY_BLOCKED",
    }
    # 预检状态 → 错误码必须是 UnifiedError 已登记码表里的码。
    from lumi_contracts import spec_for

    for code in STATUS_ERROR_CODES.values():
        if code:
            assert spec_for(code).code == code
    # 每个状态都有明确的下一步（硬拒为空 = 不给重试入口）。
    # 契约枚举里的 ``DEPENDENCY_MISSING`` 是 ``DEPENDENCY_MISSING_WORKSPACE`` 的别名
    # （同值即同一成员），规范名必须有下一步。
    for state in PreflightState:
        assert state.value in STATUS_NEXT_ACTIONS, state
    assert STATUS_NEXT_ACTIONS["DEPENDENCY_MISSING_WORKSPACE"] == "BIND_WORKSPACE"
    for state in PERMANENT_PREFLIGHT_STATES:
        assert STATUS_NEXT_ACTIONS[state] == ""


def test_action_intents_vocabulary_matches_the_plan():
    assert {item.value for item in ActionIntent} == {
        "READ", "SEARCH", "CREATE", "MODIFY", "DELETE", "EXECUTE", "SEND", "PUBLISH",
    }
    assert HIGH_RISK_SIDE_EFFECTS == {"DELETE", "EXECUTE", "PUBLISH"}


# ── TaskProfile：有效动作意图与审批判定 ─────────────────────


def test_effective_action_intents_derive_from_side_effects_only():
    assert effective_action_intents(_profile(side_effects=["WRITE"])) == ("MODIFY",)
    assert effective_action_intents(_profile(side_effects=["DELETE"])) == ("DELETE",)
    assert effective_action_intents(_profile(side_effects=["SEND"])) == ("SEND",)
    # 显式动作意图优先（新画像为准）
    explicit = _profile(side_effects=["WRITE"], action_intents=["CREATE"])
    assert effective_action_intents(explicit) == ("CREATE",)
    # 工作区依赖本身就是一次读
    assert effective_action_intents(_profile(info_sources=["WORKSPACE"])) == ("READ",)
    assert effective_action_intents(_profile()) == ()


def test_requires_approval_covers_high_risk_actions_and_flags():
    assert requires_approval(_profile(side_effects=["DELETE"])) is True
    assert requires_approval(_profile(side_effects=["EXECUTE"])) is True
    assert requires_approval(_profile(side_effects=["PUBLISH"])) is True
    assert requires_approval(_profile(action_intents=["DELETE"], side_effects=[])) is True
    assert requires_approval(_profile(risk_level="HIGH_RISK", execution_target="SANDBOX")) is True
    assert requires_approval(_profile(approval_required=True)) is True
    assert requires_approval(_profile(side_effects=["WRITE"], risk_level="REVERSIBLE")) is False


# ── Router 硬约束（§2.2）───────────────────────────────────


def test_action_intents_block_direct_chat_and_atomic_read():
    """硬约束①：动作意图非空 → 禁止 DIRECT_CHAT 与 M1_ATOMIC_READ。"""
    writable = _profile(action_intents=["CREATE"], execution_target="NONE")
    decision = route(writable)
    assert decision.mode is ExecutionMode.M1_ATOMIC_ACTION
    assert decision.mode not in {ExecutionMode.DIRECT_CHAT, ExecutionMode.M1_ATOMIC_READ}

    read = _profile(action_intents=["READ"], info_sources=["WORKSPACE"])
    assert route(read).mode is ExecutionMode.M1_ATOMIC_READ
    assert route(read).action_intents == ("READ",)


def test_legacy_read_new_profile_write_uses_the_profile():
    """**最重要的回归场景**：旧词表判只读、新画像判写入 → 以新画像为准。

    旧词表只会看到"没有副作用"（``side_effects=[]``），画像给出 ``CREATE``；
    路由必须进入受控编排（原子动作），工具窗口才有机会注入 ``workspace_write``。
    """
    profile = _profile(side_effects=[], action_intents=["CREATE"], path_determinism="KNOWN")
    decision = route(profile)
    assert decision.mode is ExecutionMode.M1_ATOMIC_ACTION
    assert decision.reason_code == "ACTION_INTENTS_REQUIRE_ORCHESTRATION"
    from app.agents.orchestration.preflight.capability_preflight import tool_window_for_actions

    assert "workspace_write" in tool_window_for_actions(decision.action_intents)


def test_high_risk_actions_require_approval_even_at_high_confidence():
    """硬约束②：DELETE / EXECUTE / PUBLISH 不因高置信度自动执行。"""
    for action in ("DELETE", "EXECUTE", "PUBLISH"):
        profile = _profile(
            confidence=1.0, action_intents=[action], path_determinism="KNOWN", execution_target="DESKTOP"
        )
        decision = route(profile)
        assert decision.approval_required is True, action
        assert decision.blocked is False, "审批不是阻断：走审批挂起而不是硬拒"
        assert decision.reason_code == "HIGH_RISK_REQUIRES_APPROVAL"
        assert task_level_action(profile) is SafetyAction.REQUIRE_USER_APPROVAL


def test_sandbox_execute_stays_reversible_without_task_level_approval():
    """沙箱是"可回滚的执行方式"：任务级不强制审批，工具级收敛为 ALLOW_SANDBOX_ONLY。"""
    profile = _profile(
        action_intents=["EXECUTE"], side_effects=["EXECUTE"], execution_target="SANDBOX",
        required_capabilities=["code.execute"], risk_level="REVERSIBLE",
    )
    assert task_level_action(profile) is SafetyAction.ALLOW
    assert route(profile).mode is ExecutionMode.M1_ATOMIC_ACTION


def test_unknown_target_asks_for_clarification_instead_of_guessing():
    """硬约束③：目标未知且有动作意图 → 澄清（不猜路径、不进编排）。"""
    profile = _profile(action_intents=["CREATE"], target_clarity="UNKNOWN")
    decision = route(profile)
    assert decision.needs_clarification is True
    assert decision.reason_code == "TARGET_CLARITY_UNKNOWN"
    assert decision.mode is None and decision.ok is False
    assert decision.suspended is True


def test_contract_projection_carries_the_new_profile_fields():
    profile = _profile(
        action_intents=["CREATE"], target_scope="WORKSPACE", target_clarity="KNOWN",
        approval_required=True, confidence_source="llm", decision_reason_code="assessor.side_effects",
    )
    contract = task_profile_contract(profile)
    assert contract.action_intents == [ActionIntent.CREATE]
    assert contract.target_scope is TargetScope.WORKSPACE
    assert contract.target_clarity is TargetClarity.KNOWN
    assert contract.approval_required is True
    assert contract.confidence_source == "llm"
    assert contract.decision_reason_code == "assessor.side_effects"

    decision = route(profile)
    contract_decision = route_decision_contract(decision, profile=profile)
    assert contract_decision.schema_version == 2
    assert contract_decision.route_mode == "m1_atomic_action"
    assert contract_decision.action_intents == ["CREATE"]
    assert contract_decision.approval_required is True
    assert contract_decision.capability_preflight is None, "未预检时不得写空结论"


# ── route_snapshot v2（§2.3）──────────────────────────────


def test_route_snapshot_v2_carries_decision_fields_and_preflight_slot():
    strict = {
        "complexity": "M1", "intent_type": "EXECUTE_ACTION", "action_intents": ["CREATE"],
        "target_scope": "WORKSPACE", "target_clarity": "KNOWN",
        "required_capabilities": ["workspace.write"], "approval_required": False,
        "confidence_source": "heuristic", "decision_reason_code": "assessor.side_effects",
    }
    snapshot = build_route_snapshot(
        router_v2_enabled=True,
        execution_policy_v2_enabled=False,
        router_meta={
            "task_profile": strict,
            "route_mode": "m1_atomic_action",
            "route_reason_code": "ACTION_INTENTS_REQUIRE_ORCHESTRATION",
            "policy_version": ROUTER_V2_POLICY_VERSION,
            "capability_preflight": {"status": "READY", "ok": True},
        },
    )
    decision = snapshot["route_decision"]
    assert decision["schema_version"] == ROUTE_DECISION_SCHEMA_VERSION == 2
    assert decision["action_intents"] == ["CREATE"]
    assert decision["target_scope"] == "WORKSPACE"
    assert decision["required_capabilities"] == ["workspace.write"]
    assert decision["capability_preflight"] == {"status": "READY", "ok": True}
    # 顶层镜像与 decision 同源
    assert snapshot["task_profile"]["action_intents"] == ["CREATE"]


def test_route_snapshot_omits_empty_preflight_slot():
    snapshot = build_route_snapshot(
        router_v2_enabled=True,
        execution_policy_v2_enabled=False,
        router_meta={"task_profile": {"complexity": "M0"}, "route_mode": "direct_chat"},
    )
    assert "capability_preflight" not in snapshot["route_decision"]


# ── 能力预检（§3）────────────────────────────────────────


def _preflight(**overrides):
    base = dict(
        profile={"action_intents": ["CREATE"], "target_clarity": "KNOWN"},
        workspace_bound=True,
        registered_tools={"workspace_write", "workspace_navigator"},
    )
    base.update(overrides)
    return preflight_capabilities(**base)


def test_security_blocked_is_a_permanent_state():
    result = _preflight(security_blocked=True)
    assert result.status == "SECURITY_BLOCKED"
    assert result.error_code == "SECURITY_BLOCKED"
    assert result.permanent is True
    assert result.safe_next_action == ""
    assert result.must_call_model is False and result.tool_window == ()


def test_plugin_disabled_capability_fails_preflight_before_the_model():
    """吸收 #1：插件被禁用 → 该能力预检直接 CAPABILITY_UNAVAILABLE。"""
    result = _preflight(
        profile={
            "action_intents": ["MODIFY"],
            "target_clarity": "KNOWN",
            "required_capabilities": ["workspace.write"],
        },
        available_capabilities={"workspace.write": False},
    )
    assert result.status == "CAPABILITY_UNAVAILABLE"
    assert result.must_call_model is False
    assert result.safe_next_action == "ENABLE_CAPABILITY"
    assert result.required_capabilities == ("workspace.write",)


def test_plugin_capability_availability_reads_the_registry():
    facts = plugin_capability_availability(["workspace.write", "code.execute", "nope.unknown"])
    assert facts["workspace.write"] is True
    assert facts["code.execute"] is True
    assert facts["nope.unknown"] is False


def test_versioned_capability_names_are_not_reported_as_missing():
    """回归：``workspace.read@1`` 曾因带 ``@1`` 被静态表判成"没有提供方"。

    目录/注册表里存的是无版本基名（``workspace.read``），画像解析出的具体能力名带契约
    版本；两处写法必须归一化后比对，否则只读任务会被误报成"缺少必需能力"并阻断。
    """
    from app.agents.capabilities.registry.resolver import (
        normalize_capability_name,
        concrete_capabilities,
    )

    # 归一化：剥版本、剥可选前缀；非数字后缀（``@beta``）不误伤。
    assert normalize_capability_name("workspace.read@1") == "workspace.read"
    assert normalize_capability_name("?code.execute@2") == "code.execute"
    assert normalize_capability_name("pkg@beta") == "pkg@beta"
    assert normalize_capability_name("") == ""

    # 画像给出的就是带版本的具体能力名——静态可用性判定必须认为它**存在**。
    concrete = concrete_capabilities(["DOCUMENT_READ"])
    assert concrete == ["workspace.read@1"]
    facts = plugin_capability_availability(concrete)
    assert facts["workspace.read@1"] is True, "带版本的能力名不得被判成缺失"

    # resolver 也要按 @N 校验契约版本（此前 parse_requirement 不认 @N，版本被忽略）。
    from app.agents.capabilities.registry.resolver import CapabilityResolver
    from app.agents.capabilities.broker.broker import capability_broker

    report = CapabilityResolver(broker=capability_broker).resolve(concrete)
    row = report.resolutions[0]
    assert row.contract_version == 1, "必须解析出 @1 的契约版本"
    assert row.capability == "workspace.read@1"


def test_no_provider_facts_distinguish_the_real_cause():
    """回归：没有可用 Provider 时不能一律说"没有可用的 Provider"。

    三种可执行原因必须分开（设备没连 / 工作区绑错 / 位置不允许），否则用户拿不到
    正确指引；对外状态仍是同一个（可重试），错误码契约不变。
    """
    from app.agents.capabilities.broker.broker import (
        PREFLIGHT_FACTS,
        _no_provider_state,
        capability_broker,
    )

    descriptor = capability_broker.catalog.get("workspace.read")
    assert descriptor is not None
    # 没有任何租约 → 设备从没注册
    assert _no_provider_state(
        [], binding=None, policy_allows_switch=False, descriptor=descriptor
    ) == "not_connected"
    # 新事实都在 Broker 的冻结词表里
    assert {
        "provider_not_connected", "provider_binding_mismatch", "provider_unroutable",
    } <= PREFLIGHT_FACTS

    # 预检按事实给出**不同的**下一步，且都阻断模型调用
    service = CapabilityPreflightService()
    for fact, expected_action in (
        ("provider_not_connected", "CONNECT_PROVIDER"),
        ("provider_binding_mismatch", "BIND_WORKSPACE"),
        ("provider_unroutable", "CHANGE_EXECUTION_PLACEMENT"),
    ):
        result = service.preflight(
            profile={"action_intents": ["READ"], "target_clarity": "KNOWN",
                     "required_capabilities": ["workspace.read@1"]},
            probe=lambda caps, f=fact: {"workspace.read@1": f},
        )
        assert result.must_call_model is False, "没有可用 Provider 时不得调用模型"
        assert result.safe_next_action == expected_action, fact
        assert result.error_code == "PROVIDER_UNHEALTHY", "对外错误码契约不变（可重试类）"
        assert result.required_capabilities == ("workspace.read@1",)


def test_preflight_snapshot_exposes_actionable_next_step():
    result = _preflight(workspace_bound=False, requires_workspace=True)
    snapshot = preflight_snapshot(result)
    assert snapshot["status"] == "DEPENDENCY_MISSING"
    assert snapshot["safe_next_action"] == "BIND_WORKSPACE"
    assert snapshot["must_call_model"] is False
    assert snapshot["tool_window"] == []
    assert snapshot["required_capabilities"] == []


def test_preflight_contract_object_round_trips():
    result = _preflight(profile={"action_intents": ["MODIFY"], "target_clarity": "UNKNOWN"})
    decision = route_decision_contract(None, preflight=result)
    preflight = decision.capability_preflight
    assert isinstance(preflight, CapabilityPreflight)
    assert preflight.state is PreflightState.NEEDS_CLARIFICATION
    assert preflight.must_call_model is False
    assert preflight.needs_human is True


# ── 预检 control 帧（§6.1：走既有事件体系，不新增事件类型）────


def test_preflight_control_frame_maps_states_without_new_event_types():
    from app.agents.orchestration.preflight.capability_preflight_service import (
        CLARIFICATION_OPTIONS,
        preflight_control_frame,
    )

    blocked = preflight_control_frame(_preflight(workspace_bound=False, requires_workspace=True))
    assert blocked is not None
    assert blocked["type"] == "control" and blocked["state"] == "blocked"
    assert blocked["phase"] == "preflight"
    assert blocked["error_code"] == "DEPENDENCY_MISSING_WORKSPACE"
    assert blocked["safe_next_action"] == "BIND_WORKSPACE"
    assert blocked["must_call_model"] is False
    assert "options" not in blocked, "硬阻断不给选项"

    clarify = preflight_control_frame(_preflight(profile={"action_intents": ["CREATE"], "target_clarity": "UNKNOWN"}))
    assert clarify is not None and clarify["state"] == "waiting_clarification"
    assert clarify["options"] == list(CLARIFICATION_OPTIONS)
    assert clarify["question"]

    approval = preflight_control_frame(_preflight(approval_required=True))
    assert approval is not None and approval["state"] == "waiting_approval"

    security = preflight_control_frame(_preflight(security_blocked=True))
    assert security is not None and security["state"] == "blocked"
    assert security["safe_next_action"] == "", "硬拒不给可执行下一步"

    # 通过时不发控制帧
    assert preflight_control_frame(_preflight()) is None


def test_preflight_control_frame_reaches_the_event_stream_and_snapshot():
    """routing 落盘 → 实时帧与刷新读取同一份载荷。"""
    from app.agents.orchestration.preflight.capability_preflight_service import attach_preflight
    from app.agents.orchestration.planning.office_plan_selection_service import (
        PREFLIGHT_CONTROL_KEY,
        preflight_control_frame_from_routing,
    )

    result = _preflight(workspace_bound=False, requires_workspace=True)
    routing = attach_preflight({}, result)
    from app.agents.orchestration.preflight.capability_preflight_service import preflight_control_frame

    routing[PREFLIGHT_CONTROL_KEY] = preflight_control_frame(result)
    frame = preflight_control_frame_from_routing(routing)
    assert frame is not None and frame["state"] == "blocked"
    assert preflight_control_frame_from_routing({}) is None


# ── execution_policy / task_shape 消费画像，不再自行解析原文 ─────


def test_execution_policy_consumes_profile_intents_instead_of_text():
    from lumi_orch.execution_policy import (
        TaskEntrySignals,
        assess_profile_from_signals,
    )

    # 同一段文本、不同画像 → 策略画像不同（说明它读的是画像而不是原文）。
    signals = TaskEntrySignals(
        request="帮我处理一下",
        reasons=(),
        action_intents=("CREATE",),
    )
    profile = assess_profile_from_signals(signals)
    assert profile["has_side_effect"] is True
    assert profile["action_intents"] == ["CREATE"]
    assert profile["safety_level"] == "SAFE_WRITE"

    readonly = assess_profile_from_signals(
        TaskEntrySignals(request="帮我处理一下", reasons=("side_effect",), action_intents=("READ",))
    )
    assert readonly["has_side_effect"] is False, "画像说只读就不是副作用（旧 reasons 只作兜底）"

    risky = assess_profile_from_signals(
        TaskEntrySignals(request="看一下", reasons=(), action_intents=("DELETE",))
    )
    assert risky["has_side_effect"] is True and risky["safety_level"] == "RISKY_WRITE"

    # 没有画像时退回旧 reasons（兼容路径不变）
    legacy = assess_profile_from_signals(TaskEntrySignals(request="删除旧目录", reasons=("side_effect",)))
    assert legacy["has_side_effect"] is True


def test_task_shape_is_now_a_profile_adapter_with_a_legacy_fallback():
    from app.agents.orchestration.planning.task_shape import (
        assess_task_shape,
        shape_from_profile,
    )

    # 画像投影：动作意图非空 ⇒ 需要编排；来源标记为 profile。
    shaped = shape_from_profile(_profile(action_intents=["CREATE"]))
    assert shaped.requires_orchestration is True
    assert shaped.source == "profile"
    assert "side_effect" in shaped.reasons

    readonly = shape_from_profile(_profile(action_intents=["READ"]))
    assert readonly.source == "profile"
    assert "side_effect" not in readonly.reasons

    # 旧正则仍是兜底路径，但明确标记来源（Phase 6 删除项）。
    legacy = assess_task_shape("在工作区创建一个 README.md")
    assert legacy.source == "legacy_regex"
    assert legacy.requires_orchestration is True


# ── react_runner 工具窗口：画像优先，关键词只兜底（§4.1）────


def test_react_runner_tool_window_scope_prefers_profile_intents():
    from app.agents.orchestration.react_runner import OfficeReactRunner

    def scope(intents, route_text):
        runner = OfficeReactRunner(user_id="u1", job_id="j1", action_intents=intents)
        return runner._intent_scope(route_text)  # noqa: SLF001 - 直测判定

    # 有画像：只读意图即使文本里出现"创建/删除"也**不**注入写工具
    write, execute, _commit = scope(["READ"], "帮我在工作区创建文件并删除旧目录")
    assert write is False and execute is False, "画像说只读就不能被文本关键词带回写路径"
    write, execute, _commit = scope(["CREATE"], "看一下项目文件")
    assert write is True and execute is False, "画像说创建就必须注入写工具"
    write, execute, _commit = scope(["EXECUTE"], "跑一下测试")
    assert execute is True and write is False
    write, execute, _commit = scope(["MODIFY", "EXECUTE"], "随便")
    assert write is True and execute is True

    # 没有画像（旧任务/开关关闭）：退回关键词兜底，行为与改造前一致
    write, execute, commit = scope([], "修改 src/main.py 并提交")
    assert write is True and commit is True
    assert scope([], "只读看看") == (False, False, False)


def test_worker_context_carries_read_only_profile_facts():
    """方案 4 §1.2：Worker 复用同一份画像事实，不重新解析用户原文。"""
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.execution.node import ApplicationTaskNodeExecutor
    from app.agents.orchestration.models import Job, JobStatus, TaskNode

    node = TaskNode(id="s1", name="步骤", agent="w1")
    job = Job(
        job_id="profile-facts",
        user_id="u1",
        request="在工作区创建 README.md",
        status=JobStatus.RUNNING,
        nodes=[node],
        routing={
            "route_decision": {
                "route_mode": "m1_atomic_action",
                "task_profile": {
                    "action_intents": ["CREATE"],
                    "target_scope": "WORKSPACE",
                    "target_clarity": "KNOWN",
                    "required_capabilities": ["workspace.write"],
                    "approval_required": False,
                },
            }
        },
    )
    executor = ApplicationTaskNodeExecutor(
        job=job, workers={}, review=None, store=None, llm_api_key=None, llm_config=None
    )
    facts = executor._task_profile_facts()  # noqa: SLF001 - 直测注入事实
    assert facts["action_intents"] == ["CREATE"]
    assert facts["target_scope"] == "WORKSPACE"
    assert facts["required_capabilities"] == ["workspace.write"]

    # 空值不进事实（不注入空壳字段）；没有画像时是空字典，绝不现场猜。
    empty = ApplicationTaskNodeExecutor(
        job=Job(job_id="j2", user_id="u1", request="x", nodes=[node], routing={}),
        workers={}, review=None, store=None, llm_api_key=None, llm_config=None,
    )
    assert empty._task_profile_facts() == {}
    assert isinstance(WorkerContext(user_id="u1", job_id="j1").task_profile, dict)


# ── 影子模式（§5）────────────────────────────────────────


def test_discrepancy_classification_prioritises_write_differences():
    assert classify_discrepancy(
        legacy_requires_orchestration=False, profile_requires_orchestration=True
    ) is DiscrepancyType.LEGACY_READ_PROFILE_WRITE
    assert classify_discrepancy(
        legacy_requires_orchestration=True, profile_requires_orchestration=False
    ) is DiscrepancyType.LEGACY_WRITE_PROFILE_READ
    assert classify_discrepancy(
        legacy_requires_orchestration=True, profile_requires_orchestration=True,
        legacy_complex=False, profile_complex=True,
    ) is DiscrepancyType.LEGACY_SIMPLE_PROFILE_COMPLEX
    assert classify_discrepancy(
        legacy_requires_orchestration=True, profile_requires_orchestration=True
    ) is DiscrepancyType.NONE
    # 任何差异都以新画像为准
    assert discrepancy_should_use_profile(DiscrepancyType.LEGACY_READ_PROFILE_WRITE) is True
    assert discrepancy_should_use_profile(DiscrepancyType.NONE) is False


def test_shadow_record_keeps_only_enums_and_bumps_no_behaviour():
    async def scenario():
        await task_shadow.reset_shadow_for_tests()
        record = task_shadow.build_record(
            job_id="shadow-job",
            legacy_requires_orchestration=False,
            profile_requires_orchestration=True,
            profile_route_mode="m1_atomic_action",
            profile_reason_code="ACTION_INTENTS_REQUIRE_ORCHESTRATION",
            confidence_source="heuristic",
            assessor_ms=12,
        )
        assert record.discrepancy_type is DiscrepancyType.LEGACY_READ_PROFILE_WRITE
        # 影子期仍走旧逻辑：记录实情，但不改行为
        assert record.selected_route_source.value == "legacy"
        assert record.profile_authoritative is False
        await task_shadow.record_shadow(record, enabled=True)
        rows = await task_shadow.load_shadow_records("shadow-job")
        assert len(rows) == 1 and str(rows[0].discrepancy_type) == "legacy_read_profile_write"
        # 序列化里没有用户原文/推理字段
        payload = rows[0].as_dict()
        assert "request" not in payload and "reasoning" not in payload
        await task_shadow.reset_shadow_for_tests()

    asyncio.run(scenario())


def test_shadow_report_aggregates_diff_and_timeout_rates():
    records = [
        task_shadow.build_record(
            job_id="j", legacy_requires_orchestration=False, profile_requires_orchestration=True,
            assessor_ms=10,
        ),
        task_shadow.build_record(
            job_id="j", legacy_requires_orchestration=True, profile_requires_orchestration=True,
            assessor_ms=30,
        ),
        task_shadow.build_record(
            job_id="j", legacy_requires_orchestration=True, profile_requires_orchestration=False,
            assessor_ms=0, assessor_timed_out=True,
        ),
    ]
    report = build_shadow_report(records)
    assert report.total == 3 and report.diff_total == 2
    assert report.assessor_timeout_total == 1
    assert report.by_type["legacy_read_profile_write"] == 1
    assert report.diff_rate == pytest.approx(2 / 3, abs=1e-4)
    assert report.timeout_rate == pytest.approx(1 / 3, abs=1e-4)
    assert report.assessor_ms_avg == 13  # (10+30+0)//3


def test_shadow_recording_is_a_noop_when_disabled():
    async def scenario():
        await task_shadow.reset_shadow_for_tests()
        record = task_shadow.build_record(
            job_id="off-job", legacy_requires_orchestration=False, profile_requires_orchestration=True
        )
        assert await task_shadow.record_shadow(record, enabled=False) is False
        assert await task_shadow.load_shadow_records("off-job") == []

    asyncio.run(scenario())


def test_plan_and_route_records_shadow_without_changing_the_route(monkeypatch):
    """影子期：给了旧词表判定就记录差异，但**路由结果不变**。"""
    from app.core.config import settings
    from app.services.task_assessor import AssessmentContext
    from app.services.task_router_adapter import plan_and_route

    monkeypatch.setattr(settings, "INTEGRATION_SHADOW_MODE", True)
    asyncio.run(task_shadow.reset_shadow_for_tests())
    try:
        routed = asyncio.run(
            plan_and_route(
                request="在工作区创建 README.md",
                context=AssessmentContext(request="x", workspace_id="w1", workspace_bound=True),
                use_llm=False,
                legacy_requires_orchestration=False,
            )
        )
        assert routed.shadow is not None, "影子模式打开且给了旧判定时必须记录"
        assert routed.shadow["selected_route_source"] == "legacy"
        assert routed.shadow["profile_requires_orchestration"] is True
        # 实际路由由画像决定（新画像为准），不是由影子记录决定
        assert routed.decision.mode is not None
        rows = asyncio.run(task_shadow.load_shadow_records(""))
        assert rows, "记录应可读回"
    finally:
        asyncio.run(task_shadow.reset_shadow_for_tests())
