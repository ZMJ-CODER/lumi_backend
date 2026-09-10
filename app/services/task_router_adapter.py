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

    def meta(self) -> dict:
        return {
            "task_profile": self.profile.model_dump(),
            "route_mode": self.decision.mode.value if self.decision.mode else "",
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
