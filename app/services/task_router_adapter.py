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
from app.services.task_assessor import AssessmentContext, assess_task_profile

BLOCKING_SAFETY = frozenset({SafetyAction.REQUIRE_ADMIN_APPROVAL, SafetyAction.BLOCK})


@dataclass(slots=True)
class RoutedTask:
    profile: TaskProfile
    decision: RouteDecision
    safety_action: SafetyAction
    assessor_source: str = "heuristic"

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
        return task_profile_contract(self.profile)

    def contract_decision(self) -> ContractRouteDecision:
        """路由决策 → 契约决策（含契约画像与旧模式串，可审计）。"""
        return route_decision_contract(self.decision, profile=self.profile)

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
        return {
            "task_profile": self.profile.model_dump(),
            "route_mode": route_mode,
            "route_reason_code": self.decision.reason_code,
            "safety_action": self.safety_action.value,
            "assessor_source": self.assessor_source,
            "policy_version": "router_v2",
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
) -> RoutedTask:
    """评估画像 → 8 步路由 → 任务级风控；任何异常回退确定性保守画像。"""
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
    bound = context.workspace_bound or bool(context.workspace_id) if workspace_bound is None else workspace_bound
    decision = route(
        profile,
        security_violated=security_violated,
        workspace_bound=bool(bound),
        service_authorized=service_authorized,
    )
    return RoutedTask(
        profile=profile,
        decision=decision,
        safety_action=task_level_action(profile),
        assessor_source=source,
    )


__all__ = ["BLOCKING_SAFETY", "RoutedTask", "plan_and_route"]
