"""路由契约桥接（第三阶段）：``lumi_orch`` 路由事实 → ``lumi_contracts`` 契约。

为什么需要桥接而不是直接把 ``lumi_orch`` 的类型换成契约类型：

* ``lumi_orch.task_assessment.TaskProfile`` 是**严格评估画像**（M0~M3、
  ``path_determinism``、``estimated_steps``…），路由内核依赖它，不能在契约层
  再复制一份"必须永远同步"的字段表；
* ``lumi_contracts.routing.TaskProfile`` 是**跨模块可审计画像**（粗粒度、
  显式枚举、可序列化），模型/UI/验收日志消费它。

因此约定：``lumi_orch`` 继续做"路由事实 + 8 步决策"的唯一实现，本模块把它
投影成契约对象（``TaskProfile`` / ``RouteDecision`` / ``ExecutionRequest``），
并在**一个地方**登记旧枚举串与新枚举的对应关系。旧字符串仍然照原样输出，
契约字段只增不改，避免影响既有快照/前端。

链路::

    lumi_orch.task_assessment.TaskProfile      （事实）
      → lumi_contracts.routing.TaskProfile     （可审计画像）
    lumi_orch.execution_router.RouteDecision   （选择）
      → lumi_contracts.routing.RouteDecision   （可审计决策）
      → lumi_contracts.routing.ExecutionRequest（本次执行要做什么）
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from lumi_contracts import (
    ActionIntent,
    CapabilityPreflight,
    Complexity,
    ConfidenceSource,
    ExecutionRequest,
    ExecutionTarget,
    InfoSource,
    IntentType,
    RouteDecision,
    RouteMode,
    Sensitivity,
    ServerContext,
    TargetClarity,
    TargetScope,
    TaskProfile,
    normalize_preflight_state,
)

# ── 旧枚举串 ↔ 契约枚举（唯一登记处）──────────────────────────────
#
# ``lumi_orch.execution_router.ExecutionMode`` 的取值是历史快照字段
# （``routing["route_mode"]``、``task_router`` SSE 事件）的一部分，前端与验收
# 日志都在消费，因此**保留原字符串**；契约 ``RouteMode`` 只作为内部类型视图。
LEGACY_MODE_BY_CONTRACT: dict[RouteMode, str] = {
    RouteMode.DIRECT_CHAT: "direct_chat",
    RouteMode.ATOMIC_READ: "m1_atomic_read",
    RouteMode.SINGLE_ACTION: "m1_atomic_action",
    RouteMode.PLANNER_DAG: "sequential_workflow",
    RouteMode.REACT: "dynamic_agent",
    RouteMode.BLOCKED: "",
}

CONTRACT_MODE_BY_LEGACY: dict[str, RouteMode] = {
    legacy: contract for contract, legacy in LEGACY_MODE_BY_CONTRACT.items() if legacy
}

# 复杂度：评估用 M0~M3，契约用 ATOMIC/SEQUENTIAL/DYNAMIC。
_COMPLEXITY_BY_LEGACY: dict[str, Complexity] = {
    "M0": Complexity.ATOMIC,
    "M1": Complexity.ATOMIC,
    "M2": Complexity.SEQUENTIAL,
    "M3": Complexity.DYNAMIC,
}

# 信息来源：契约把 EXTERNAL_WEB 收敛为 PUBLIC_WEB。
_INFO_SOURCE_BY_LEGACY: dict[str, InfoSource] = {
    "USER_PROVIDED": InfoSource.USER_PROVIDED,
    "CONVERSATION_MEMORY": InfoSource.CONVERSATION_MEMORY,
    "INTERNAL_KNOWLEDGE": InfoSource.INTERNAL_KNOWLEDGE,
    "WORKSPACE": InfoSource.WORKSPACE,
    "ATTACHED_FILE": InfoSource.ATTACHED_FILE,
    "EXTERNAL_WEB": InfoSource.PUBLIC_WEB,
    "PUBLIC_WEB": InfoSource.PUBLIC_WEB,
    "PRIVATE_SERVICE": InfoSource.PRIVATE_SERVICE,
    "SYSTEM_STATE": InfoSource.SYSTEM_STATE,
}

# 执行位置：契约只有 SERVER（后端）/DESKTOP/SANDBOX/NONE；第三方服务由后端发起。
_EXECUTION_TARGET_BY_LEGACY: dict[str, ExecutionTarget] = {
    "NONE": ExecutionTarget.NONE,
    "BACKEND": ExecutionTarget.SERVER,
    "SERVER": ExecutionTarget.SERVER,
    "DESKTOP": ExecutionTarget.DESKTOP,
    "SANDBOX": ExecutionTarget.SANDBOX,
    "EXTERNAL_SERVICE": ExecutionTarget.SERVER,
}


def _enum_value(value: Any) -> str:
    """``StrEnum`` / ``Enum`` / 裸字符串统一取字符串值。"""
    return str(getattr(value, "value", value) or "")


def task_profile_contract(profile: Any) -> TaskProfile:
    """评估画像（``lumi_orch``）→ 契约画像。

    粗粒度契约放不下的路由事实（``path_determinism`` / ``estimated_steps`` /
    ``data_sensitivity``…）全部进 ``debug``，**不静默丢弃**；无法识别的新来源
    也会被记录下来，而不是猜一个枚举值。
    """
    legacy_complexity = _enum_value(getattr(profile, "complexity", ""))
    raw_sources = list(getattr(profile, "info_sources", None) or getattr(profile, "required_sources", None) or [])
    sources: list[InfoSource] = []
    unmapped: list[str] = []
    for item in raw_sources:
        name = _enum_value(item)
        mapped = _INFO_SOURCE_BY_LEGACY.get(name)
        if mapped is None:
            unmapped.append(name)
            continue
        sources.append(mapped)
    if not sources:
        sources = [InfoSource.USER_PROVIDED]
    if unmapped:
        logger.debug("路由画像存在未映射的信息来源（已记入 debug）: {}", ",".join(unmapped))
    target = _enum_value(getattr(profile, "execution_target", ""))
    side_effects_raw = getattr(profile, "side_effects", None)
    if isinstance(side_effects_raw, bool):
        has_side_effects = side_effects_raw
    else:
        has_side_effects = bool(side_effects_raw) or bool(getattr(profile, "has_side_effect", False))
    side_effect_facts = (
        [_enum_value(item) for item in side_effects_raw]
        if isinstance(side_effects_raw, (list, tuple, set, frozenset))
        else []
    )
    intent_type = _enum_value(getattr(profile, "intent_type", ""))
    goal = str(getattr(profile, "goal", "") or "")
    if not goal:
        goal = "EXECUTE" if intent_type == "EXECUTE_ACTION" else "GENERATE"
    # ── 方案 4 §1.1：动作意图等新字段（内核画像已带则原样上抛，缺失则按零副作用留空）──
    action_intents = [str(_enum_value(item)) for item in (getattr(profile, "action_intents", None) or [])]
    action_intents = [item for item in action_intents if item]
    return TaskProfile(
        goal=goal,
        complexity=_COMPLEXITY_BY_LEGACY.get(legacy_complexity, Complexity.ATOMIC),
        side_effects=bool(has_side_effects),
        info_sources=sources,
        output_target=_enum_value(getattr(profile, "output_target", "")),
        execution_target=_EXECUTION_TARGET_BY_LEGACY.get(target, ExecutionTarget.NONE),
        required_capabilities=[str(item) for item in (getattr(profile, "required_capabilities", None) or [])],
        risk_level=_enum_value(getattr(profile, "risk_level", "")) or "low",
        confidence=float(getattr(profile, "confidence", 0.0) or 0.0),
        intent_type=IntentType.EXECUTE_ACTION if intent_type == "EXECUTE_ACTION" else IntentType.GENERATE_ONLY,
        action_intents=_action_intents(action_intents),
        target_scope=_target_scope(getattr(profile, "target_scope", ""), sources),
        target_clarity=_target_clarity(getattr(profile, "target_clarity", "")),
        has_dependency=bool(getattr(profile, "has_dependency", False)),
        has_runtime_decision=bool(getattr(profile, "has_runtime_decision", False))
        or _enum_value(getattr(profile, "complexity", "")) == "M3",
        approval_required=bool(getattr(profile, "approval_required", False)),
        confidence_source=_confidence_source(getattr(profile, "confidence_source", "")),
        decision_reason_code=str(getattr(profile, "decision_reason_code", "") or ""),
        debug={
            "legacy_complexity": legacy_complexity,
            "legacy_execution_target": target,
            "intent_type": intent_type,
            "path_determinism": _enum_value(getattr(profile, "path_determinism", "")),
            "estimated_steps": getattr(profile, "estimated_steps", None),
            "data_sensitivity": _enum_value(getattr(profile, "data_sensitivity", "")),
            "context_size_estimate": _enum_value(getattr(profile, "context_size_estimate", "")),
            "side_effects": side_effect_facts,
            "unmapped_info_sources": unmapped,
        },
    )


def _action_intents(values: list[str]) -> list[ActionIntent]:
    """动作意图字符串 → 契约枚举（非法值丢弃；不猜、不兜底成 WRITE）。"""
    out: list[ActionIntent] = []
    for item in values:
        try:
            parsed = ActionIntent(str(item).strip().upper())
        except ValueError:
            continue
        if parsed not in out:
            out.append(parsed)
    return out


def _target_scope(value: Any, sources: list[InfoSource]) -> TargetScope:
    """目标范围：优先画像显式值，缺失时按信息源推导（只读事实，不解析原文）。"""
    try:
        return TargetScope(str(_enum_value(value)).strip().upper())
    except ValueError:
        pass
    if InfoSource.WORKSPACE in sources:
        return TargetScope.WORKSPACE
    if InfoSource.ATTACHED_FILE in sources:
        return TargetScope.ATTACHMENT
    if {InfoSource.PUBLIC_WEB, InfoSource.PRIVATE_SERVICE} & set(sources):
        return TargetScope.EXTERNAL_SERVICE
    if InfoSource.SYSTEM_STATE in sources:
        return TargetScope.SYSTEM_STATE
    return TargetScope.USER_INPUT


def _target_clarity(value: Any) -> TargetClarity:
    try:
        return TargetClarity(str(_enum_value(value)).strip().upper())
    except ValueError:
        return TargetClarity.KNOWN


def _confidence_source(value: Any) -> ConfidenceSource:
    try:
        return ConfidenceSource(str(_enum_value(value)).strip().lower())
    except ValueError:
        return ConfidenceSource.HEURISTIC


def route_decision_contract(
    decision: Any,
    *,
    profile: Any = None,
    contract_profile: TaskProfile | None = None,
    preflight: Any = None,
) -> RouteDecision:
    """路由决策（``lumi_orch``）→ 契约决策（方案 4 §2.3 schema v2）。

    ``mode`` 保留为契约枚举（历史视图）；``route_mode`` 是内核同名词表；旧字符串放在
    ``signals["legacy_mode"]``，需要写快照/事件时用 :func:`legacy_route_mode` 取回，
    保证既有输出不变。新增字段全部来自画像/决策，**不重新解析用户原文**。

    :param contract_profile: 已经投影好的契约画像（避免重复投影，语义不变）。
    :param preflight: 预检结论（``CapabilityPreflight`` / dict / 结果对象），可选。
    """
    blocked = bool(getattr(decision, "blocked", False))
    legacy_mode = _enum_value(getattr(getattr(decision, "mode", None), "value", getattr(decision, "mode", "")))
    mode = CONTRACT_MODE_BY_LEGACY.get(legacy_mode)
    if mode is None:
        mode = RouteMode.BLOCKED if blocked else RouteMode.DIRECT_CHAT
    resolved_profile = contract_profile or (task_profile_contract(profile) if profile is not None else None)
    reason = str(getattr(decision, "reason", "") or "")
    reason_code = str(getattr(decision, "reason_code", "") or "")
    return RouteDecision(
        mode=mode,
        reason=reason,
        profile=resolved_profile,
        signals={
            "legacy_mode": legacy_mode,
            "reason_code": reason_code,
            "blocked": blocked,
            "approval_required": bool(getattr(decision, "approval_required", False)),
            "needs_clarification": bool(getattr(decision, "needs_clarification", False)),
        },
        required_capabilities=list(resolved_profile.required_capabilities) if resolved_profile else [],
        blocked_reason=reason if blocked else "",
        # ── schema v2（加法）──
        route_mode=legacy_mode,
        intent_type=str(getattr(decision, "intent_type", "") or (str(resolved_profile.intent_type) if resolved_profile else "")),
        action_intents=[str(item) for item in (getattr(decision, "action_intents", None) or (resolved_profile.action_intents if resolved_profile else ()))],
        target_scope=str(resolved_profile.target_scope) if resolved_profile else "",
        target_clarity=str(resolved_profile.target_clarity) if resolved_profile else "",
        approval_required=bool(getattr(decision, "approval_required", False))
        or bool(getattr(resolved_profile, "approval_required", False)),
        needs_clarification=bool(getattr(decision, "needs_clarification", False)),
        confidence=float(getattr(resolved_profile, "confidence", 0.0) or 0.0),
        confidence_source=str(getattr(resolved_profile, "confidence_source", "")),
        decision_reason_code=reason_code
        or str(getattr(resolved_profile, "decision_reason_code", "") or ""),
        capability_preflight=_preflight_contract(preflight),
    )


def _preflight_contract(preflight: Any) -> CapabilityPreflight | None:
    """预检结论 → 契约对象（接受契约对象 / 结果对象 / dict；无法识别返回 ``None``）。"""
    if preflight is None:
        return None
    if isinstance(preflight, CapabilityPreflight):
        return preflight
    if hasattr(preflight, "as_dict"):
        payload = preflight.as_dict()
        if isinstance(payload, dict):
            return _preflight_from_mapping(payload)
        return None
    if isinstance(preflight, dict):
        return _preflight_from_mapping(preflight)
    return None


def _preflight_from_mapping(payload: dict[str, Any]) -> CapabilityPreflight | None:
    """把预检结果字典收敛成契约对象（``status`` 与 ``state`` 两种键名都认）。

    历史状态名（``DEPENDENCY_MISSING``）经契约的别名归一，避免旧快照读不出来。
    """
    state = str(payload.get("state") or payload.get("status") or "").strip()
    if not state:
        return None
    normalized = normalize_preflight_state(state)
    if normalized is None:
        return None
    return CapabilityPreflight(
        state=normalized,
        error_code=str(payload.get("error_code") or ""),
        safe_message=str(payload.get("safe_message") or ""),
        safe_next_action=str(payload.get("safe_next_action") or payload.get("next_action") or ""),
        question=str(payload.get("question") or ""),
        options=[str(item) for item in (payload.get("options") or [])],
        tool_window=[str(item) for item in (payload.get("tool_window") or [])],
        must_call_model=bool(payload.get("must_call_model", normalized.ok)),
        checks=[dict(item) for item in (payload.get("checks") or []) if isinstance(item, dict)],
        required_capabilities=[str(item) for item in (payload.get("required_capabilities") or [])],
    )


def _coerce_route_mode(mode: RouteMode | str) -> RouteMode:
    if isinstance(mode, RouteMode):
        return mode
    text = str(mode or "")
    for item in RouteMode:
        if item.value == text:
            return item
    return RouteMode.DIRECT_CHAT


def legacy_route_mode(mode: RouteMode | str | None) -> str:
    """契约决策模式 → 历史快照用的 ``route_mode`` 字符串。"""
    if mode is None:
        return ""
    return LEGACY_MODE_BY_CONTRACT.get(_coerce_route_mode(mode), "")


def execution_request(
    instruction: str,
    *,
    context: ServerContext,
    route: RouteDecision | None = None,
    allowed_tools: tuple[str, ...] = (),
    denied_tools: tuple[str, ...] = (),
    max_steps: int = 1,
    max_chars: int = 0,
    data_sensitivity: Sensitivity | None = None,
    idempotency_key: str = "",
    approval_fingerprint: str = "",
) -> ExecutionRequest:
    """构造本次执行请求（身份字段只来自服务端 ``ServerContext``）。

    故意不提供 ``from_arguments()`` 之类的入口：模型/插件传入的身份与审批
    状态一律不可信。
    """
    return ExecutionRequest(
        instruction=str(instruction or ""),
        context=context,
        route=route,
        allowed_tools=tuple(str(item) for item in allowed_tools),
        denied_tools=tuple(str(item) for item in denied_tools),
        max_steps=max(1, int(max_steps or 1)),
        max_chars=max(0, int(max_chars or 0)),
        data_sensitivity=data_sensitivity or context.data_sensitivity,
        idempotency_key=str(idempotency_key or ""),
        approval_fingerprint=str(approval_fingerprint or ""),
    )


def execution_request_snapshot(request: ExecutionRequest) -> dict[str, Any]:
    """执行请求的**可审计快照**（不含正文，供 Job 快照/验收日志读取）。

    只保留"这次执行被授权做什么"：路由模式、允许/禁止的工具、步数上限、数据
    敏感级与关联标识摘要；``instruction`` 只留长度与哈希，不重复存用户原文。
    """
    import hashlib

    instruction = str(request.instruction or "")
    route = getattr(request, "route", None)
    mode = getattr(route, "mode", None)
    return {
        "schema_name": "lumi.execution_request",
        "schema_version": 1,
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:32],
        "instruction_chars": len(instruction),
        "route_mode": str(getattr(mode, "value", mode) or ""),
        "allowed_tools": list(request.allowed_tools),
        "denied_tools": list(request.denied_tools),
        "max_steps": int(request.max_steps or 1),
        "max_chars": int(request.max_chars or 0),
        "data_sensitivity": str(request.data_sensitivity),
        "idempotency_key": str(request.idempotency_key or ""),
        "has_approval_fingerprint": bool(request.approval_fingerprint),
        "workspace_bound": bool(request.context.workspace_id),
        "conversation_bound": bool(request.context.conversation_id),
    }


__all__ = [
    "CONTRACT_MODE_BY_LEGACY",
    "LEGACY_MODE_BY_CONTRACT",
    "execution_request",
    "execution_request_snapshot",
    "legacy_route_mode",
    "route_decision_contract",
    "task_profile_contract",
]
