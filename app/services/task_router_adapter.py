"""app 适配层：Assessor → ExecutionRouter → SafetyGuard（任务级）一次成型的决策。

只做“组装修配”，不含策略本身：策略在 lumi_orch（task_assessment /
execution_router / safety_policy）。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from lumi_orch.execution_router import ExecutionMode, RouteDecision, route
from lumi_orch.safety_policy import SafetyAction, task_level_action
from lumi_orch.task_assessment import TaskProfile

# 阶段三：编排边界的所有对外形状都来自契约（旧枚举串只经映射表还原）。
from app.contracts.routing import (
    RouteDecision as ContractRouteDecision,
    ServerContext,
    TaskProfile as ContractTaskProfile,
    execution_request,
    legacy_route_mode,
    route_decision_contract,
    task_profile_contract,
)
from app.contracts import ExecutionRequest
from app.services import task_assessor as task_assessor_module
from app.services.task_assessor import AssessmentContext, assess_task_profile

BLOCKING_SAFETY = frozenset({SafetyAction.REQUIRE_ADMIN_APPROVAL, SafetyAction.BLOCK})


@dataclass(slots=True)
class RoutedTask:
    profile: TaskProfile
    decision: RouteDecision
    safety_action: SafetyAction
    assessor_source: str = "heuristic"
    #: 预检结论（方案 4 §2.3：随路由快照落盘；``None`` = 尚未预检）。
    capability_preflight: dict | None = None
    #: 契约画像（缓存：`route_snapshot` 与出口都要用，避免重复投影产生漂移）。
    _contract_profile: ContractTaskProfile | None = None
    #: 旧词表判定（影子模式比对用；``None`` = 没有旧判定可对比）。
    legacy_requires_orchestration: bool | None = None
    #: 影子差异记录快照（仅影子模式打开且给了旧判定时有值）。
    shadow: dict | None = None

    @property
    def mode(self) -> ExecutionMode | None:
        return self.decision.mode

    @property
    def blocked(self) -> bool:
        if self.decision.blocked:
            return True
        return self.safety_action in BLOCKING_SAFETY

    @property
    def blocked_reason(self) -> str:
        if self.decision.blocked:
            return self.decision.reason
        if self.safety_action in BLOCKING_SAFETY:
            return "该任务风险等级过高，已被安全策略拦截；请联系管理员或在沙箱/回收站等可逆方式下重试。"
        return ""

    # ── 阶段三：编排边界对外只暴露契约对象 ────────────────────────
    def contract_profile(self) -> ContractTaskProfile:
        """评估画像 → 契约画像（``lumi_contracts.routing.TaskProfile``）。"""
        if self._contract_profile is None:
            self._contract_profile = task_profile_contract(self.profile)
        return self._contract_profile

    def contract_decision(self) -> ContractRouteDecision:
        """路由决策 → 契约决策（含契约画像、v2 字段与预检位，可审计）。"""
        return route_decision_contract(
            self.decision,
            profile=self.profile,
            contract_profile=self.contract_profile(),
            preflight=self.capability_preflight,
        )

    def execution_request(
        self,
        instruction: str,
        *,
        context: ServerContext,
        **overrides,
    ) -> ExecutionRequest:
        """本次执行请求（身份来自服务端上下文，路由来自契约决策）。"""
        return execution_request(
            instruction,
            context=context,
            route=self.contract_decision(),
            **overrides,
        )

    def meta(self) -> dict:
        # 旧快照字段全部保留：``route_mode`` 由契约决策经唯一映射表还原，
        # 不再各自硬编码字符串。
        route_mode = legacy_route_mode(self.contract_decision().mode) if self.decision.mode is not None else ""
        profile_payload = self.profile.model_dump()
        # 契约画像的 §1.1 字段是**权威**（内核画像可能只有旧词表），合并时以契约为准。
        contract_profile = self.contract_profile()
        for key in ("action_intents", "target_scope", "target_clarity", "approval_required",
                    "confidence_source", "decision_reason_code", "has_dependency", "has_runtime_decision"):
            value = getattr(contract_profile, key, None)
            if value is None:
                continue
            payload_value = value if isinstance(value, (str, bool, int, float)) else [str(item) for item in value]
            if payload_value not in ("", [], False) or key == "approval_required":
                profile_payload[key] = payload_value
        return {
            "task_profile": profile_payload,
            "route_mode": route_mode,
            "route_reason_code": self.decision.reason_code,
            "safety_action": self.safety_action.value,
            "assessor_source": self.assessor_source,
            "policy_version": "router_v2",
            # 方案 4：预检位与"是否需人工介入"随快照一起落盘（缺省不写空对象）。
            "approval_required": bool(self.decision.approval_required),
            "needs_clarification": bool(self.decision.needs_clarification),
            **({"capability_preflight": dict(self.capability_preflight)} if self.capability_preflight else {}),
        }


async def plan_and_route(
    *,
    request: str,
    context: AssessmentContext,
    user_id: str = "",
    llm_api_key: str | None = None,
    llm_config: dict | None = None,
    use_llm: bool = True,
    security_violated: bool = False,
    workspace_bound: bool | None = None,
    service_authorized: bool = True,
    legacy_requires_orchestration: bool | None = None,
) -> RoutedTask:
    """评估画像 → 8 步路由 → 任务级风控；任何异常回退确定性保守画像。

    画像**只在这里评估一次**（方案 4 §1.2 单一入口）：路由、策略、工具窗口、Job 提交
    全部复用同一份 ``TaskProfile``，任何模块不得再次解析用户原文判复杂度/意图。

    :param legacy_requires_orchestration: 旧词表（``task_shape``）的判定结果，仅供
        影子模式比对；**不参与**路由决策（新画像为准）。
    """
    assessor_ms = 0
    started = None
    try:
        from app.services.task_shadow import elapsed_ms, timing_started

        started = timing_started()
    except Exception:  # noqa: BLE001 - 影子工具不可用不影响路由
        started = None
    try:
        profile, source = await assess_task_profile(
            context,
            user_id=user_id,
            llm_api_key=llm_api_key,
            llm_config=llm_config,
            use_llm=use_llm,
        )
    except Exception as exc:  # noqa: BLE001 - 适配层必须兜底
        logger.warning("任务评估失败，使用确定性保守画像: {}", str(exc)[:200])
        from app.services.task_assessor import heuristic_profile

        profile, source = heuristic_profile(context), "heuristic"
    if started is not None:
        try:
            from app.services.task_shadow import elapsed_ms

            assessor_ms = elapsed_ms(started)
        except Exception:  # noqa: BLE001
            assessor_ms = 0
    profile = _with_canonical_semantics(context, profile, source=source)
    bound = context.workspace_bound or bool(context.workspace_id) if workspace_bound is None else workspace_bound
    decision = route(
        profile,
        security_violated=security_violated,
        workspace_bound=bool(bound),
        service_authorized=service_authorized,
    )
    shadow = await _record_shadow(
        job_id=str(getattr(context, "job_id", "") or getattr(context, "conversation_id", "") or ""),
        profile=profile,
        decision=decision,
        legacy_requires_orchestration=legacy_requires_orchestration,
        assessor_ms=assessor_ms,
        confidence_source=str(getattr(profile, "confidence_source", "") or ""),
    )
    return RoutedTask(
        profile=profile,
        decision=decision,
        safety_action=task_level_action(profile),
        assessor_source=source,
        legacy_requires_orchestration=legacy_requires_orchestration,
        shadow=shadow,
    )


async def _record_shadow(
    *,
    job_id: str,
    profile: TaskProfile,
    decision: RouteDecision,
    legacy_requires_orchestration: bool | None,
    assessor_ms: int,
    confidence_source: str,
):
    """影子模式的**结构化差异记录**（方案 §5.1）。

    只有调用方给了旧词表判定（``legacy_requires_orchestration``）且影子开关打开时才记录：
    没有旧判定就没有"新旧对比"这回事，不能凭空造一条"一致"的记录污染统计。

    影子期不改变实际路由（``profile_authoritative=False``，仍走旧逻辑）；记录内容只有
    枚举与耗时，不含用户原文。
    """
    if legacy_requires_orchestration is None:
        return None
    try:
        from app.services import task_shadow

        if not task_shadow.shadow_enabled():
            return None
        profile_requires = decision.mode is not None and not decision.blocked
        record = task_shadow.build_record(
            job_id=job_id,
            legacy_requires_orchestration=bool(legacy_requires_orchestration),
            profile_requires_orchestration=bool(profile_requires),
            # 旧词表只有"要不要编排"，没有具体模式：只记新侧模式，旧侧留空。
            profile_route_mode=str(getattr(decision.mode, "value", decision.mode) or ""),
            profile_reason_code=str(decision.reason_code or ""),
            confidence_source=confidence_source,
            assessor_ms=assessor_ms,
            legacy_complex=str(getattr(profile, "complexity", "") or ""),
            profile_complex=str(getattr(profile, "complexity", "") or ""),
        )
        await task_shadow.record_shadow(record)
        return task_shadow.shadow_snapshot(record)
    except Exception as exc:  # noqa: BLE001 - 影子记录绝不影响路由
        logger.debug("[task-shadow] 记录失败（忽略）: {}", str(exc)[:120])
        return None


def _with_canonical_semantics(context: AssessmentContext, profile: TaskProfile, *, source: str) -> TaskProfile:
    """把 canonical 画像的 §1.1 字段（动作意图/目标范围/清晰度）合并进内核画像。

    只在 ``TASK_PROFILE_CANONICAL`` 打开时合并；关闭时返回原画像（**行为完全不变**）。
    合并的是"事实字段"，路由硬约束才能真正生效——否则内核画像永远只有旧词表的
    ``side_effects``，"旧词表判只读、新画像判写入"这一类冲突无法被纠正。
    """
    try:
        from app.platform.runtime.feature_flags import feature_enabled

        if not feature_enabled(task_assessor_module.CANONICAL_FLAG):
            return profile
    except Exception:  # noqa: BLE001 - 开关不可用时保持旧行为（保守）
        return profile
    try:
        canonical = task_assessor_module.canonical_profile(context, source=source, legacy=profile)
    except Exception as exc:  # noqa: BLE001 - canonical 失败不能拖垮路由
        logger.warning("canonical 画像合并失败，保持内核画像: {}", str(exc)[:160])
        return profile
    updates: dict = {}
    for key in (
        "action_intents",
        "target_scope",
        "target_clarity",
        "approval_required",
        "confidence_source",
        "decision_reason_code",
        "has_dependency",
        "has_runtime_decision",
    ):
        value = getattr(canonical, key, None)
        if value is None:
            continue
        if isinstance(value, list):
            values = [str(getattr(item, "value", item)) for item in value]
            if values:
                updates[key] = values
            continue
        text = str(getattr(value, "value", value))
        if key == "approval_required":
            updates[key] = bool(value)
        elif text:
            updates[key] = text
    if not updates:
        return profile
    try:
        return profile.model_copy(update=updates)
    except Exception as exc:  # noqa: BLE001 - 字段不合法时保持原画像
        logger.warning("canonical 字段合并被拒绝（保持内核画像）: {}", str(exc)[:160])
        return profile


__all__ = ["BLOCKING_SAFETY", "RoutedTask", "plan_and_route"]
