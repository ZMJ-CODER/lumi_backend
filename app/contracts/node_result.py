"""节点结果 → ``ExecutionResult``（结果归一化链的唯一入口）。

方案 §二.3 要求：

    原始结果 → ExecutionResult → UI Projection → Stream Event

不允许 `ToolOutput` 直接序列化上 SSE、`SkillResult` 直接拼字符串、模型原文直接进气泡。
本模块补上"节点/工具结果进入契约"的那一步，之后所有"结果类"事件（产物、视图、
失败详情）都从 ``ExecutionResult`` 派生，而不是各自读 Job 快照上的散字段。

只映射**安全字段**：``node.result`` 里的 ``content`` / ``output`` / ``step_title`` /
``outputs`` 是既有展示字段；工具原始参数、原始响应、模型推理不在 ``node.result`` 里，
也不会在这里被读取。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts import ArtifactRef, ExecutionResult
from lumi_contracts.common.status import ExecutionStatus
from lumi_contracts.execution import ExecutionTiming
from lumi_contracts.execution.result import failure as failure_result
from lumi_contracts.execution.result import ok as ok_result

#: 节点状态 → 契约执行状态（未知值按 failed 收敛，不发明新状态）。
_NODE_STATUS: dict[str, ExecutionStatus] = {
    "pending": ExecutionStatus.PENDING,
    "ready": ExecutionStatus.PENDING,
    "running": ExecutionStatus.PENDING,
    "retrying": ExecutionStatus.PENDING,
    "waiting_approval": ExecutionStatus.PENDING_APPROVAL,
    "completed": ExecutionStatus.SUCCESS,
    "skipped": ExecutionStatus.EMPTY,
    "failed": ExecutionStatus.FAILED,
    "error": ExecutionStatus.FAILED,
    "cancelled": ExecutionStatus.CANCELLED,
    "interrupted": ExecutionStatus.CANCELLED,
}


def _status_of(node: Any) -> ExecutionStatus:
    raw = getattr(node, "status", "")
    text = str(getattr(raw, "value", raw) or "").strip().casefold()
    return _NODE_STATUS.get(text, ExecutionStatus.FAILED)


def _duration_ms(node: Any) -> int:
    started = getattr(node, "started_at", None)
    completed = getattr(node, "completed_at", None)
    try:
        if started is None or completed is None:
            return 0
        return max(0, int((float(completed) - float(started)) * 1000))
    except (TypeError, ValueError):
        return 0


def artifacts_of_node(node: Any, *, container_id: str) -> list[dict[str, Any]]:
    """节点产物引用（安全元数据 + 签名 ``artifact_id``），供事件与快照复用。"""
    result = getattr(node, "result", None)
    if not isinstance(result, dict):
        return []
    from app.services.artifacts import artifact_from_output

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result.get("outputs") or []:
        if not isinstance(item, dict):
            continue
        ref = artifact_from_output(container_id, item)
        if ref is None or ref["artifact_id"] in seen:
            continue
        seen.add(ref["artifact_id"])
        out.append(ref)
    return out[:50]


def execution_result_from_node(
    node: Any,
    *,
    container_id: str,
    user_id: str = "",
) -> ExecutionResult[Any]:
    """节点结果 → ``ExecutionResult``（结果归一化链的落点）。

    * ``artifact_refs``：契约 ``ArtifactRef``（``ref_id`` = 签名 artifact_id）；
    * ``metadata["artifacts"]``：同一批产物的**安全展示元数据**（含 ``expires_at``），
      事件载荷从这里取字段，避免为了展示字段去反解契约引用；
    * 失败节点产出 ``failure`` 信封（错误码稳定，不新造错误结构）。
    """
    status = _status_of(node)
    result = getattr(node, "result", None)
    result = result if isinstance(result, dict) else {}
    tool_name = str(
        result.get("tool")
        or (getattr(node, "params", None) or {}).get("preferred_tool")
        or getattr(node, "agent", "")
        or ""
    )
    call_id = str(result.get("call_id") or getattr(node, "id", "") or "")
    timing = ExecutionTiming(duration_ms=_duration_ms(node))
    if status in {ExecutionStatus.PENDING, ExecutionStatus.PENDING_APPROVAL}:
        return ExecutionResult[Any](
            status=status,
            tool_name=tool_name,
            call_id=call_id,
            node_id=str(getattr(node, "id", "") or ""),
            timing=timing,
        )
    if status in {ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
        error = failure_result(
            str(getattr(node, "error_code", "") or "NODE_EXECUTION_FAILED"),
            str(getattr(node, "error", "") or "步骤未完成"),
            tool_name=tool_name,
            status=status,
        )
        return error.model_copy(update={
            "call_id": call_id,
            "node_id": str(getattr(node, "id", "") or ""),
            "timing": timing,
        })

    artifacts = artifacts_of_node(node, container_id=container_id)
    refs = [
        ArtifactRef(
            ref_id=item["artifact_id"],
            name=item["filename"],
            media_type=item["mime_type"],
            size=item.get("size_bytes"),
            # 保留策略随引用一起走（契约字段；前端据此显示到期提示）。
            retention_class=str(item.get("retention_class") or ""),
            requested_expires_at=str(item.get("requested_expires_at") or ""),
            effective_expires_at=str(item.get("effective_expires_at") or ""),
            retention_policy_source=str(item.get("retention_policy_source") or ""),
            retention_clamp_reason=str(item.get("retention_clamp_reason") or ""),
        )
        for item in artifacts
    ]
    payload = {
        "summary": str(result.get("content") or result.get("output") or result.get("step_title") or "")[:2000],
        "outputs": [{"name": item["filename"], "size": item.get("size_bytes")} for item in artifacts],
    }
    return ok_result(
        payload,
        tool_name=tool_name,
        call_id=call_id,
        output=str(result.get("content") or result.get("output") or ""),
        artifact_refs=refs,
        metadata={"artifacts": artifacts, "step_title": str(result.get("step_title") or "")},
        status=status,
    ).model_copy(update={
        "node_id": str(getattr(node, "id", "") or ""),
        "timing": timing,
    })


__all__ = ["artifacts_of_node", "execution_result_from_node"]
