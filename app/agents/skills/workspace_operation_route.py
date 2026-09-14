"""executor 与统一操作契约之间的薄接线（放在 skills 侧，避免契约层反向依赖）。

为什么需要单独一层：``app/contracts/operations`` 与 ``app/workspace/write/operations``
都不该 import ``app.agents.skills``（会形成循环），而 executor 需要把
``OperationResult`` 折成既有 ``SkillResult``。转换与上下文注入因此留在调用方一侧。

插入位置（``execute_tool_call`` 里，参数校验之后、能力租约门禁**之前**）：

    routed = await try_workspace_operation(...)
    if routed is not None:
        return routed          # 已由操作网关执行（或结构化失败/待审批）

为什么在租约门禁**之前**：四个操作能力的执行是服务端操作网关（版本校验 + 审批 +
回收站 + 读回校验），而租约门禁只按"客户端是否广告了该能力"派发——客户端只广告
``workspace.write@1``，让门禁先介入会把 ``workspace.edit/move/delete`` 一律判成
``CAPABILITY_MISSING``。审批并没有被绕过：这里仍然走**同一套** ``policy_guard``
指纹校验（见 :func:`_authorize`），通过后 ``OperationContext.approval_state`` 才置为
``approved``。
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.contracts.operations import (
    OperationApprovalState,
    OperationContext,
    OperationStatus,
)
from app.workspace.write.operations import (
    NavigatorWorkspaceClient,
    WorkspaceOperationService,
    operation_kind_for_tool,
)

#: 操作结果状态 → ``SkillResult.status``（既有词表：success/failed/pending_approval/...）。
_SKILL_STATUS = {
    OperationStatus.SUCCESS.value: "success",
    OperationStatus.NO_CHANGE.value: "success",
    OperationStatus.ALREADY_ABSENT.value: "success",
    OperationStatus.PENDING_APPROVAL.value: "pending_approval",
    OperationStatus.DENIED.value: "failed",
    OperationStatus.FAILED.value: "failed",
}


def _skill_result(
    *,
    status: str,
    output: str = "",
    data: Any = None,
    error: str = "",
    error_code: str = "",
    retryable: bool = False,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """构造既有 ``SkillResult``（= ToolOutput）。

    注意**不能**同时传 ``success``：既有信封的 ``model_post_init`` 会按 ``success``
    覆盖 ``status``（``success=False`` → ``failed``），那样 "pending_approval" 会被
    悄悄改写成普通失败，审批入口就没了。这里只给 ``status``，让信封自己推导 ``success``。
    """
    from app.agents.skills.base import SkillResult

    return SkillResult(
        status=status,
        output=output,
        data=data,
        content_type="structured" if data is not None else "text",
        error=error or None,
        error_code=error_code or None,
        retryable=bool(retryable),
        metadata=dict(metadata or {}),
    )


def _authorize(
    descriptor: Any,
    *,
    capability: str,
    args: dict[str, Any],
    approval_context: dict[str, Any] | None,
) -> tuple[bool, Any | None]:
    """审批门禁（与 ``CapabilityDispatchAdapter`` 同一套策略与指纹校验）。

    返回 ``(approved, failure)``：``approved=True`` 表示可以执行；否则 ``failure`` 是
    结构化的 ``CapabilityResult``（需要补审批/指纹不符）。
    """
    from lumi_contracts.plugins import CapabilityInvocation

    try:
        from app.agents.capabilities.policy.policy_guard import policy_guard

        context = dict(approval_context or {})
        invocation = CapabilityInvocation(
            capability=capability,
            arguments=dict(args or {}),
            request_id=str(context.get("request_id") or ""),
        )
        verdict, failure = policy_guard.authorize(descriptor, invocation, approval_context=context or None)
    except Exception as exc:  # noqa: BLE001 - 门禁异常按"未审批"处理（保守）
        logger.warning("[operation] 审批门禁异常（按未审批处理）: {}", str(exc)[:160])
        return False, None
    if failure is not None and not failure.ok:
        return False, failure
    return True, None


async def try_workspace_operation(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    user_id: str,
    user_role: str = "user",
    conversation_id: str = "",
    workspace_id: str = "",
    device_id: str = "",
    task_id: str | None = None,
    call_id: str | None = None,
    approved_tool_calls: Any = None,
    upstream_sha256: str = "",
    client_factory: Any = None,
) -> Any | None:
    """工具名命中操作契约时执行操作网关；否则返回 ``None``（走旧路径）。

    任何内部异常都收敛为 ``None``（旧路径继续跑）——这一层是新增能力，不是新的单点故障。
    """
    kind = operation_kind_for_tool(tool_name)
    if kind is None:
        return None
    payload = dict(args or {})
    if not str(workspace_id or "").strip():
        return _skill_result(
            status="failed",
            error="当前任务没有已选择的工作区；请先在办公模式中新建或打开项目",
            error_code="WORKSPACE_SCOPE_REQUIRED",
            metadata={"tool": tool_name, "operation": str(kind)},
        )
    try:
        from app.agents.capabilities.catalog.legacy import capability_catalog

        capability = f"workspace.{kind}"
        descriptor = capability_catalog.get(capability)
        approval_context = _approval_context(
            tool_name=tool_name,
            args=payload,
            capability=capability,
            approved_tool_calls=approved_tool_calls,
            upstream_sha256=upstream_sha256,
        )
        approved = False
        if descriptor is not None:
            approved, failure = _authorize(
                descriptor, capability=capability, args=payload, approval_context=approval_context
            )
            if not approved:
                code = str(getattr(failure, "error_code", "") or "APPROVAL_REQUIRED")
                message = str(getattr(getattr(failure, "error", None), "message", "") or "需要确认后继续")
                suggested = str(getattr(getattr(failure, "error", None), "suggested_action", "") or "")
                return _skill_result(
                    status="pending_approval" if code == "APPROVAL_REQUIRED" else "failed",
                    error=message,
                    error_code=code,
                    retryable=bool(getattr(failure, "retryable", False)),
                    metadata={
                        "tool": tool_name,
                        "operation": str(kind),
                        "capability": capability,
                        "approval_state": "pending" if code == "APPROVAL_REQUIRED" else "rejected",
                        "suggested_action": suggested,
                    },
                )
        context = OperationContext.from_source(
            AgentExecutionContext.from_metadata(
                user_id=user_id,
                user_role=user_role,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                device_id=device_id,
            ),
            idempotency_key=_idempotency_key(tool_name=tool_name, args=payload, call_id=call_id),
            approval_state=(
                OperationApprovalState.APPROVED if approved else OperationApprovalState.NOT_REQUIRED
            ),
            step_id=str(task_id or ""),
        )
        service = WorkspaceOperationService(
            (client_factory or _default_client)(
                user_id=user_id,
                user_role=user_role,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                request=str(payload.get("request") or ""),
            ),
            context=context,
        )
        started = time.perf_counter()
        result = await service.execute(kind, payload)
        result = result.model_copy(
            update={"duration_ms": int((time.perf_counter() - started) * 1000)}
        )
        error = result.error
        return _skill_result(
            status=_SKILL_STATUS.get(str(result.status), "failed"),
            output=result.summary_text(),
            data=result.to_dict(),
            error=(error.message if error else ""),
            error_code=(error.code if error else ""),
            retryable=bool(error.retryable) if error else False,
            metadata={
                **result.tool_metadata(tool_name=tool_name),
                "capability": capability,
                "operation_summary": {
                    "operation": str(result.kind),
                    "status": str(result.status),
                    "logical_path": result.logical_path,
                    "target_path": result.target_path,
                    "revision": result.new_revision or result.old_revision,
                    "changed_files": result.affected_files,
                    "approval_state": str(result.approval_state),
                    "rollback_available": result.rollback_available,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - 操作层异常落回旧路径并记录
        logger.warning(
            "[operation] 操作网关异常（落回旧路径）tool={} err={}",
            str(tool_name)[:60],
            str(exc)[:200],
        )
        return None


def _default_client(**kwargs: Any) -> NavigatorWorkspaceClient:
    from app.workspace.read.navigator import WorkspaceNavigatorService

    return NavigatorWorkspaceClient(
        WorkspaceNavigatorService(
            user_id=str(kwargs.get("user_id") or ""),
            user_role=str(kwargs.get("user_role") or "user"),
            workspace_id=str(kwargs.get("workspace_id") or ""),
            conversation_id=str(kwargs.get("conversation_id") or ""),
            request=str(kwargs.get("request") or ""),
        )
    )


def _idempotency_key(*, tool_name: str, args: dict[str, Any], call_id: str | None) -> str:
    """幂等键：调用方显式给了就用它，否则用本次调用的 call_id（同一调用重放安全）。"""
    explicit = str(args.get("idempotency_key") or "").strip()
    if explicit:
        return explicit
    return str(call_id or "").strip()


def _approval_context(
    *,
    tool_name: str,
    args: dict[str, Any],
    capability: str,
    approved_tool_calls: Any,
    upstream_sha256: str,
) -> dict[str, Any] | None:
    """由既有工具级审批推导能力审批上下文（未命中 → None = 未审批）。"""
    try:
        from app.agents.skills.capability_route import _approval_context_for

        return _approval_context_for(
            tool_name=tool_name,
            args=args,
            capability=capability,
            approved_tool_calls=approved_tool_calls,
            upstream_sha256=upstream_sha256,
        )
    except Exception as exc:  # noqa: BLE001 - 推导失败按未审批处理
        logger.debug("[operation] 审批上下文推导失败: {}", str(exc)[:120])
        return None


__all__ = ["try_workspace_operation"]
