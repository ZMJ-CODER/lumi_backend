"""阶段 3 后端：executor 分支前的能力路由门禁（feature flag + 分阶段切流）。

接入形状（在 ``workspace_navigator`` / ``sandbox_*`` 分支**之前**）：

    decision = await maybe_route_capability(tool_name, args, ctx, ...)
    if decision.handled:
        return decision.result          # 已按租约派发
    # 否则走原现网路径（decision 里带着 shadow 打点信息）

三个阶段（``AGENT_CAPABILITY_ROUTING_MODE``）：

==============  ==============================================================
``off``         完全不介入（连能力查询都不做）
``shadow``      查能力/租约/健康并**打点**"本应路由到哪个 Provider"，不改执行路径
``read_only``   只读能力（workspace.read）真正走 Broker 派发；写/执行仍走旧路径
``active``      全部已声明能力都走派发；写/执行失败**结构化失败**，不静默回退
==============  ==============================================================

为什么写/执行最后切：它们的失败面更大（半截副作用），且审批链路更敏感；只读先切能
在不冒风险的前提下验证"注册→租约→派发"整条链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from lumi_contracts.plugins import CapabilityResult

from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.broker.dispatch import (
    NEVER_FALLBACK_CAPABILITIES,
    CapabilityDispatchAdapter,
    capability_for_mcp_tool,
)

#: 派发模式。
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_READ_ONLY = "read_only"
MODE_ACTIVE = "active"
VALID_MODES: frozenset[str] = frozenset({MODE_OFF, MODE_SHADOW, MODE_READ_ONLY, MODE_ACTIVE})

#: 只读阶段允许真正派发的能力（写/执行不在其中）。
READ_ONLY_CAPABILITIES: frozenset[str] = frozenset(
    {"workspace.read", "workspace.list", "workspace.search"}
)

#: 历史别名 → 规范模式（配置里写哪个都能用）。
_MODE_ALIASES: dict[str, str] = {
    "": MODE_OFF,
    "none": MODE_OFF,
    "disabled": MODE_OFF,
    "workspace_read_only": MODE_READ_ONLY,
    "readonly": MODE_READ_ONLY,
    "write": MODE_ACTIVE,
    "code": MODE_ACTIVE,
    "full": MODE_ACTIVE,
}


def normalize_mode(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text in VALID_MODES:
        return text
    return _MODE_ALIASES.get(text, MODE_OFF)


def routing_mode() -> str:
    try:
        from app.core.config import settings

        return normalize_mode(getattr(settings, "AGENT_CAPABILITY_ROUTING_MODE", MODE_OFF))
    except Exception:  # noqa: BLE001 - 配置不可用时按"不介入"处理
        return MODE_OFF


@dataclass(slots=True)
class RoutingDecision:
    """一次门禁结论（``handled=True`` 时用 ``result`` 直接返回）。"""

    handled: bool = False
    mode: str = MODE_OFF
    capability: str = ""
    tool_name: str = ""
    result: CapabilityResult | None = None
    provider_id: str = ""
    lease_id: str = ""
    reason: str = ""
    shadow: bool = False
    #: 统一资源能力层（Phase 3）：派发目标的结构化字段（排障/前端展示用）。
    unified_capability: str = ""
    resource_type: str = ""
    provider_name: str = ""
    #: shadow 模式下的打点（进过程日志/审计）。
    observation: dict[str, Any] = field(default_factory=dict)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "handled": bool(self.handled),
            "mode": self.mode,
            "capability": self.capability,
            "tool_name": self.tool_name,
            "provider_id": self.provider_id,
            "lease_id": self.lease_id,
            "reason": self.reason,
            "shadow": bool(self.shadow),
            "unified_capability": self.unified_capability,
            "resource_type": self.resource_type,
            "provider_name": self.provider_name,
            "observation": dict(self.observation),
        }


def should_route(mode: str, capability: str) -> bool:
    """该模式下这个能力是否**真正**走派发（shadow 永远 False）。"""
    if mode == MODE_OFF or mode == MODE_SHADOW:
        return False
    if mode == MODE_ACTIVE:
        return True
    return capability in READ_ONLY_CAPABILITIES


async def maybe_route_capability(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    context: AgentExecutionContext,
    lease_service: Any,
    adapter: CapabilityDispatchAdapter | None = None,
    mode: str = "",
    timeout_s: float | None = None,
    task_id: str | None = None,
    call_id: str | None = None,
    approval_context: dict[str, Any] | None = None,
    capability: str = "",
    unified_capability: str = "",
    resource_type: str = "",
    provider_name: str = "",
) -> RoutingDecision:
    """executor 分支前的统一入口。

    * ``off``：直接返回 ``handled=False``（零开销，不查任何东西）；
    * ``shadow``：查询并返回打点，**handled 仍为 False**；
    * ``read_only`` / ``active``：命中的能力交给适配层派发，`handled=True`。

    ``capability`` 给定时**不再从工具名猜能力**（Phase 3）：统一资源能力层已经解析出
    结构化目标（能力 / 资源类型 / Provider），这里只把"资源类型允许的 Provider"传给
    适配层收窄候选。留空时保持旧行为（``capability_for_mcp_tool`` 兼容解析）。
    """
    resolved = normalize_mode(mode or routing_mode())
    structured = bool(capability)
    if not structured:
        capability = capability_for_mcp_tool(tool_name) or ""
    if resolved == MODE_OFF or not capability:
        return RoutingDecision(
            mode=resolved,
            tool_name=tool_name,
            capability=capability or "",
            unified_capability=unified_capability,
            resource_type=resource_type,
            provider_name=provider_name,
        )

    provider_ids: frozenset[str] | None = None
    if structured and unified_capability and resource_type:
        try:
            from app.agents.capabilities.broker.resource_dispatch import provider_ids_for

            provider_ids = provider_ids_for(unified_capability, resource_type) or None
        except Exception as exc:  # noqa: BLE001 - 收窄失败按不收窄处理
            logger.debug("[capability] Provider 收窄集合推导失败: {}", str(exc)[:120])

    active = should_route(resolved, capability)
    if adapter is None:
        adapter = CapabilityDispatchAdapter(lease_service=lease_service)
    outcome = await adapter.dispatch(
        capability=capability,
        args=args,
        context=context,
        tool_name=tool_name,
        # 只读能力允许回退旧路径；写/执行不允许（适配层内部还会再判一次）。
        allow_legacy_fallback=capability not in NEVER_FALLBACK_CAPABILITIES,
        timeout_s=timeout_s,
        task_id=task_id,
        call_id=call_id,
        approval_context=approval_context,
        provider_ids=provider_ids,
    )
    observation = outcome.to_snapshot()
    if unified_capability or resource_type:
        observation = {
            **observation,
            "unified_capability": unified_capability,
            "resource_type": resource_type,
            "provider_name": provider_name,
        }
    if not active:
        logger.info(
            "[capability][shadow] tool={} capability={} unified={} resource={} "
            "本应路由 provider={} lease={} reason={} handled_would_be={}",
            tool_name,
            capability,
            unified_capability or "-",
            resource_type or "-",
            outcome.provider_id or "-",
            outcome.lease_id or "-",
            outcome.reason,
            outcome.handled,
        )
        return RoutingDecision(
            handled=False,
            mode=resolved,
            capability=capability,
            tool_name=tool_name,
            provider_id=outcome.provider_id,
            lease_id=outcome.lease_id,
            reason=outcome.reason,
            shadow=True,
            unified_capability=unified_capability,
            resource_type=resource_type,
            provider_name=provider_name,
            observation=observation,
        )
    return RoutingDecision(
        handled=outcome.handled,
        mode=resolved,
        capability=capability,
        tool_name=tool_name,
        result=outcome.result,
        provider_id=outcome.provider_id,
        lease_id=outcome.lease_id,
        reason=outcome.reason,
        unified_capability=unified_capability,
        resource_type=resource_type,
        provider_name=provider_name,
        observation=observation,
    )


__all__ = [
    "MODE_ACTIVE",
    "MODE_OFF",
    "MODE_READ_ONLY",
    "MODE_SHADOW",
    "READ_ONLY_CAPABILITIES",
    "RoutingDecision",
    "VALID_MODES",
    "maybe_route_capability",
    "normalize_mode",
    "routing_mode",
    "should_route",
]
