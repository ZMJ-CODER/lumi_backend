"""executor 与能力路由之间的薄接线（放在 skills 侧，避免能力层反向依赖）。

为什么单独一个模块：能力包不能 import ``app.agents.skills``（否则形成
capabilities → skills → capabilities 的循环），而 executor 需要把
``CapabilityResult`` 折成既有 ``SkillResult``。因此转换逻辑放在**调用方一侧**。

插入位置（``execute_tool_call`` 里，参数校验与资源策略之后、MCP 分支之前）：

    routed = await try_capability_route(...)
    if routed is not None:
        return routed          # 已按租约派发（或结构化失败）
    # 否则落回原现网路径

模式语义见 ``app.agents.capabilities.routing``：``off`` 零开销、``shadow`` 只打点、
``read_only`` 只切只读、``active`` 全切。
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from app.agents.capabilities.context import AgentExecutionContext
from app.agents.capabilities.dispatch import capability_for_mcp_tool
from app.agents.capabilities.routing import (
    MODE_OFF,
    RoutingDecision,
    maybe_route_capability,
    routing_mode,
)


def _skill_result(
    *,
    success: bool,
    output: str = "",
    data: Any = None,
    error: str = "",
    error_code: str = "",
    retryable: bool = False,
    metadata: dict[str, Any] | None = None,
) -> Any:
    from app.agents.skills.base import SkillResult

    return SkillResult(
        status="success" if success else "failed",
        success=success,
        output=output,
        data=data,
        content_type="structured" if data is not None else "text",
        error=error or None,
        error_code=error_code or None,
        retryable=retryable,
        metadata=dict(metadata or {}),
    )


def skill_result_from_capability(
    decision: RoutingDecision,
    *,
    tool_name: str,
) -> Any:
    """``RoutingDecision``（含 ``CapabilityResult``）→ 既有 ``SkillResult``。

    保留全部关键语义，便于上游/前端复用既有渲染：
    ``error_code`` 原样（含 ``POLICY_DENIED``/``LEASE_EXPIRED``/``PROVIDER_UNHEALTHY``）、
    ``retryable`` 原样、``served_locally`` 与 ``provider_id``/``lease_id`` 进 metadata。
    """
    result = decision.result
    metadata: dict[str, Any] = {
        "tool": tool_name,
        "capability": decision.capability,
        "capability_routed": True,
        "capability_mode": decision.mode,
        "provider_id": decision.provider_id,
        "lease_id": decision.lease_id,
    }
    if result is None:
        return _skill_result(
            success=False,
            error="能力派发未返回结果",
            error_code="CAPABILITY_DISPATCH_FAILED",
            metadata=metadata,
        )
    metadata["served_locally"] = bool(result.served_locally)
    if result.ok:
        payload = result.payload if isinstance(result.payload, dict) else {}
        # 客户端返回的信封里 ``content`` 是给模型看的正文；``data`` 保留结构化结果。
        text = str(payload.get("content") or payload.get("output") or "")
        return _skill_result(
            success=True,
            output=text,
            data=result.payload,
            metadata=metadata,
        )
    envelope = result.error
    return _skill_result(
        success=False,
        error=str(getattr(envelope, "message", "") or result.error_code or "能力调用失败"),
        error_code=result.error_code or "CAPABILITY_FAILED",
        retryable=result.retryable,
        metadata=metadata | {"suggested_action": str(getattr(envelope, "suggested_action", "") or "")},
    )


async def try_capability_route(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    user_id: str,
    user_role: str = "user",
    conversation_id: str = "",
    workspace_id: str = "",
    device_id: str = "",
    authorized_project_ids: Any = None,
    lease_service: Any = None,
    mode: str = "",
    task_id: str | None = None,
    call_id: str | None = None,
    approved_tool_calls: Any = None,
    upstream_sha256: str = "",
) -> Any | None:
    """尝试按租约派发；返回 ``SkillResult``（已接管/结构化失败）或 ``None``（走旧路径）。

    ``approved_tool_calls`` 是**本次调用已获批准的确切指纹集合**（既有工具级审批）。
    命中时把它翻译成能力审批上下文交给 Broker 门禁；未命中则按"未审批"处理——需要审批
    的写/执行能力会返回 ``APPROVAL_REQUIRED``，而不是偷偷执行。

    任何内部异常都**降级为 None**（走旧路径并记录），绝不让能力路由把一次工具调用打挂
    ——它是旁路，不是新的单点故障。
    """
    resolved = mode or routing_mode()
    if resolved == MODE_OFF:
        # 零开销：连上下文都不构造。
        return None
    try:
        context = AgentExecutionContext.from_metadata(
            user_id=user_id,
            user_role=user_role,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            device_id=device_id,
            project_ids=authorized_project_ids or (),
            confirmed_tool_calls=approved_tool_calls or (),
        )
        capability = capability_for_mcp_tool(tool_name)
        approval_context = _approval_context_for(
            tool_name=tool_name,
            args=args,
            capability=capability,
            approved_tool_calls=approved_tool_calls,
            upstream_sha256=upstream_sha256,
        )
        decision = await maybe_route_capability(
            tool_name=tool_name,
            args=args,
            context=context,
            lease_service=lease_service,
            mode=resolved,
            task_id=task_id,
            call_id=call_id,
            approval_context=approval_context,
        )
    except Exception as exc:  # noqa: BLE001 - 旁路失败必须落回旧路径
        logger.warning(
            "[capability] 路由门禁异常（落回旧路径）tool={} err={}", str(tool_name)[:60], str(exc)[:160]
        )
        return None
    if decision.shadow:
        # shadow：只打点（maybe_route_capability 已记日志），不改执行路径。
        return None
    if not decision.handled:
        return None
    return skill_result_from_capability(decision, tool_name=tool_name)


def _approval_context_for(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    capability: str | None,
    approved_tool_calls: Any,
    upstream_sha256: str,
) -> dict[str, Any] | None:
    """由既有工具级审批推导能力审批上下文（未命中返回 None = 未审批）。"""
    if not capability:
        return None
    from app.agents.capabilities.approvals import binding_for_tool_call

    try:
        return binding_for_tool_call(
            tool_name,
            args,
            upstream_sha256=upstream_sha256,
            capability=capability,
            approved_tool_calls=approved_tool_calls,
        )
    except Exception as exc:  # noqa: BLE001 - 推导失败按未审批处理（保守）
        logger.debug("[capability] 审批上下文推导失败: {}", str(exc)[:120])
        return None


def record_route_observation(decision: RoutingDecision, *, tool_name: str) -> None:
    """把 shadow 打点写进过程日志（可选；调用方决定是否需要）。

    只记能力名/Provider/租约/原因，不含参数正文——与过程日志的安全约束一致。
    """
    if not decision.observation:
        return
    logger.info(
        "[capability] route_observation tool={} capability={} handled={} provider={} lease={} at={}",
        str(tool_name)[:60],
        decision.capability,
        decision.handled,
        decision.provider_id or "-",
        decision.lease_id or "-",
        time.time(),
    )


__all__ = [
    "record_route_observation",
    "skill_result_from_capability",
    "try_capability_route",
]
