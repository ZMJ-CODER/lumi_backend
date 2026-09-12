"""``OperationResult``：四个工作区操作工具的统一结果信封（**纯契约**）。

链路（方案第 1 条）：

    原始 Provider 结果
      → OperationResult（本模块：状态 + 版本 + 变化 + 审计标识）
      → ToolOutput 兼容投影（``to_tool_output``：唯一执行信封，不新增平行信封）
      → Model / UI / Audit 投影（既有 tool_output_pipeline 负责）

因此本模块**不**定义第二套执行信封：它只提供"操作语义"这一层的 payload，并通过
``to_tool_output()`` 折进现有 :class:`~app.agents.skills.output_contract.ToolOutput`。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.operations.common import (
    ChangeSummary,
    OperationApprovalState,
    OperationContext,
    OperationKind,
    OperationStatus,
    RollbackState,
)
from app.contracts.operations.errors import OperationError, operation_error

#: 操作状态 → ToolOutput 状态（既有信封词表：success/partial/pending_approval/failed/...）。
_TOOL_STATUS: dict[str, str] = {
    OperationStatus.SUCCESS.value: "success",
    # 内容没变 / 本来就不存在都是"目标已达成"：用 success 交付，绝不让前端弹确认框。
    OperationStatus.NO_CHANGE.value: "success",
    OperationStatus.ALREADY_ABSENT.value: "success",
    OperationStatus.PENDING_APPROVAL.value: "pending_approval",
    OperationStatus.DENIED.value: "failed",
    OperationStatus.FAILED.value: "failed",
}


class OperationResult(BaseModel):
    """一次工作区操作的统一结果。

    只放**控制面**信息（状态、版本、变化摘要、审批、审计标识）；文件正文永远不在这里，
    需要正文的调用方走读取能力。``error`` 只表达错误，不承担状态表达。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: OperationKind = OperationKind.WRITE
    status: OperationStatus = OperationStatus.SUCCESS
    operation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    logical_path: str = ""
    target_path: str = ""
    old_revision: str = ""
    new_revision: str = ""
    #: 客户端上报的工作区单调版本（写操作的 base_version 与结果版本都记它）。
    workspace_version: int = 0
    approval_state: OperationApprovalState = OperationApprovalState.NOT_REQUIRED
    rollback_state: RollbackState = RollbackState.NOT_APPLICABLE
    changes: ChangeSummary = Field(default_factory=ChangeSummary)
    error: OperationError | None = None
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    #: 影响面统计（删除/移动的审批依据；``files`` 是受影响文件数，含目录递归）。
    stats: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0

    # ── 审计/关联标识（服务端注入）──
    request_id: str = ""
    trace_id: str = ""
    job_id: str = ""
    node_id: str = ""
    step_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    workspace_id: str = ""
    device_id: str = ""
    provider_id: str = ""
    lease_id: str = ""
    idempotency_key: str = ""

    meta: dict[str, Any] = Field(default_factory=dict)

    # ── 派生 ──────────────────────────────────────────────

    @property
    def ok(self) -> bool:
        return self.status.is_ok

    @property
    def requires_approval(self) -> bool:
        return self.status is OperationStatus.PENDING_APPROVAL

    @property
    def rollback_available(self) -> bool:
        return self.rollback_state is RollbackState.AVAILABLE

    @property
    def affected_files(self) -> list[str]:
        return self.changes.affected_files

    def summary_text(self) -> str:
        """模型/前端可读的一行摘要（只含路径与计数，不含正文）。"""
        path = self.logical_path or self.target_path or "（未指定路径）"
        kind_label = {
            OperationKind.WRITE.value: "写入",
            OperationKind.EDIT.value: "编辑",
            OperationKind.MOVE.value: "移动",
            OperationKind.DELETE.value: "删除",
            OperationKind.RESTORE.value: "恢复",
            OperationKind.PURGE.value: "清理回收站",
        }.get(str(self.kind), str(self.kind))
        if self.status is OperationStatus.NO_CHANGE:
            return f"{kind_label}未产生变化：{path} 内容与目标一致（revision {self.new_revision or self.old_revision}）"
        if self.status is OperationStatus.ALREADY_ABSENT:
            return f"{kind_label}：{path} 本来就不存在（无需处理）"
        if self.status is OperationStatus.PENDING_APPROVAL:
            return f"{kind_label}已准备就绪，等待确认：{path}"
        if self.status is OperationStatus.DENIED:
            reason = self.error.message if self.error else "被拒绝"
            return f"{kind_label}被拒绝：{path}（{reason}）"
        if self.status is OperationStatus.FAILED:
            code = self.error.code if self.error else ""
            message = self.error.message if self.error else "执行失败"
            return f"{kind_label}失败：{path}（{code}：{message}）"
        stats = self.stats or {}
        extra = ""
        if self.kind is OperationKind.MOVE and self.target_path:
            extra = f" → {self.target_path}"
        elif stats.get("files"):
            extra = f"（{int(stats.get('files') or 0)} 个文件）"
        revision = f"，revision {self.new_revision}" if self.new_revision else ""
        return f"{kind_label}完成：{path}{extra}{revision}"

    # ── 兼容投影 ──────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe 载荷（ToolOutput.data / CapabilityResult.payload 都用它）。"""
        payload = self.model_dump(mode="json", exclude_none=True)
        # UI/前端高频字段提到顶层，避免前端为了拿状态去翻嵌套。
        payload["operation"] = str(self.kind)
        payload["revision"] = self.new_revision or self.old_revision
        payload["changed_files"] = self.affected_files
        payload["rollback_available"] = self.rollback_available
        return payload

    def tool_metadata(self, *, tool_name: str = "", **extra: Any) -> dict[str, Any]:
        """ToolOutput.metadata：审计与决策信号（不含正文）。"""
        metadata: dict[str, Any] = {
            "tool": tool_name or f"workspace_{self.kind}",
            "operation": str(self.kind),
            "operation_id": self.operation_id,
            "status": str(self.status),
            "logical_path": self.logical_path,
            "target_path": self.target_path,
            "old_revision": self.old_revision,
            "new_revision": self.new_revision,
            "workspace_version": int(self.workspace_version),
            "approval_state": str(self.approval_state),
            "requires_approval": self.requires_approval,
            "rollback_state": str(self.rollback_state),
            "rollback_available": self.rollback_available,
            "changed_files": self.affected_files,
            "affected_files": self.affected_files,
            "dry_run": bool(self.dry_run),
            "duration_ms": int(self.duration_ms),
            "idempotency_key": self.idempotency_key,
            "provider_id": self.provider_id,
            "lease_id": self.lease_id,
            "workspace_id": self.workspace_id,
            "device_id": self.device_id,
            "conversation_id": self.conversation_id,
            "job_id": self.job_id,
            "step_id": self.step_id,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "decision_signals": {
                "result_count": len(self.affected_files),
                "more_available": False,
                "truncated": False,
                "no_change": self.status is OperationStatus.NO_CHANGE,
                "already_absent": self.status is OperationStatus.ALREADY_ABSENT,
            },
        }
        metadata.update({key: value for key, value in extra.items() if value is not None})
        return metadata

    def to_tool_output(self, *, tool_name: str = "", **extra: Any):
        """→ 既有 ``ToolOutput``（**唯一执行信封**，不新增平行结果类型）。

        延迟 import：契约层不依赖 ``app.agents.*``，但投影出口必须是同一个信封。
        """
        from app.agents.skills.output_contract import ToolOutput
        from app.agents.skills.output_contract import OutputMeta

        status = _TOOL_STATUS.get(str(self.status), "failed")
        meta = self.tool_metadata(tool_name=tool_name, **extra)
        error = self.error
        return ToolOutput(
            status=status,
            data=self.to_dict(),
            content_type="structured",
            output=self.summary_text(),
            error=(error.message if error and not self.ok else None),
            error_code=(error.code if error and not self.ok else None),
            retryable=bool(error.retryable) if error else False,
            metadata=meta,
            meta=OutputMeta(
                summary=self.summary_text(),
                workspace_id=self.workspace_id or None,
                workspace_version=int(self.workspace_version) or None,
                idempotency_key=self.idempotency_key or None,
                quality_hints={
                    "operation": str(self.kind),
                    "status": str(self.status),
                    "approval_state": str(self.approval_state),
                    "rollback_available": self.rollback_available,
                    "result_count": len(self.affected_files),
                },
            ),
        )

    def to_capability_result(self, *, capability: str = ""):
        """→ ``CapabilityResult``（Broker/Provider 边界用同一个载荷）。"""
        from lumi_contracts.plugins import (
            CapabilityErrorCode,
            capability_failure,
            capability_ok,
        )

        if self.ok or self.status is OperationStatus.PENDING_APPROVAL:
            return capability_ok(
                self.to_dict(),
                capability=capability,
                provider_id=self.provider_id,
                request_id=self.request_id or self.operation_id,
                trace_id=self.trace_id,
                stream_cursor="",
                # 操作网关在服务端编排（原子落盘仍由客户端工具执行）：如实记录实际来源。
                execution_plane="server",
                runtime_kind="in_process",
            )
        error = self.error
        code = error.capability_code if error else CapabilityErrorCode.FAILED.value
        return capability_failure(
            code,
            (error.message if error else self.summary_text()),
            capability=capability,
            provider_id=self.provider_id,
            retryable=bool(error.retryable) if error else False,
            suggested_action=(error.safe_next_action if error else ""),
            details={
                "operation": str(self.kind),
                "operation_status": str(self.status),
                "logical_path": self.logical_path,
                "operation_error": error.code if error else "",
            },
        )


# ── 构造器（四个工具共用，语义固定）────────────────────────────────


def _from_context(
    ctx: OperationContext | None,
    *,
    kind: OperationKind,
    **fields: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {"kind": kind}
    if ctx is not None:
        base.update(
            {
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "job_id": ctx.job_id,
                "node_id": ctx.node_id,
                "step_id": ctx.step_id,
                "conversation_id": ctx.conversation_id,
                "user_id": ctx.user_id,
                "workspace_id": ctx.workspace_id,
                "device_id": ctx.device_id,
                "provider_id": ctx.provider_id,
                "lease_id": ctx.lease_id,
                "idempotency_key": ctx.idempotency_key,
                "workspace_version": ctx.workspace_version,
                "dry_run": ctx.dry_run,
                "approval_state": ctx.approval_state,
            }
        )
    base.update(fields)
    return base


def success_result(
    ctx: OperationContext | None = None,
    *,
    kind: OperationKind,
    logical_path: str = "",
    target_path: str = "",
    old_revision: str = "",
    new_revision: str = "",
    changes: ChangeSummary | None = None,
    rollback_state: RollbackState = RollbackState.AVAILABLE,
    stats: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    duration_ms: int = 0,
    **fields: Any,
) -> OperationResult:
    return OperationResult(
        **_from_context(
            ctx,
            kind=kind,
            status=OperationStatus.SUCCESS,
            logical_path=logical_path,
            target_path=target_path,
            old_revision=old_revision,
            new_revision=new_revision,
            changes=changes or ChangeSummary(),
            rollback_state=rollback_state,
            stats=dict(stats or {}),
            warnings=list(warnings or []),
            duration_ms=int(duration_ms),
            **fields,
        )
    )


def no_change_result(
    ctx: OperationContext | None = None,
    *,
    kind: OperationKind,
    logical_path: str = "",
    target_path: str = "",
    revision: str = "",
    duration_ms: int = 0,
    **fields: Any,
) -> OperationResult:
    """内容/目标状态已一致：**不是失败**，也不触发审批确认。"""
    return OperationResult(
        **_from_context(
            ctx,
            kind=kind,
            status=OperationStatus.NO_CHANGE,
            logical_path=logical_path,
            target_path=target_path,
            old_revision=revision,
            new_revision=revision,
            approval_state=OperationApprovalState.NOT_REQUIRED,
            rollback_state=RollbackState.NOT_NEEDED,
            duration_ms=int(duration_ms),
            **fields,
        )
    )


def already_absent_result(
    ctx: OperationContext | None = None,
    *,
    kind: OperationKind,
    logical_path: str = "",
    duration_ms: int = 0,
    **fields: Any,
) -> OperationResult:
    """删除目标本来就不存在：幂等成功态（不是错误）。"""
    return OperationResult(
        **_from_context(
            ctx,
            kind=kind,
            status=OperationStatus.ALREADY_ABSENT,
            logical_path=logical_path,
            approval_state=OperationApprovalState.NOT_REQUIRED,
            rollback_state=RollbackState.NOT_NEEDED,
            stats={"files": 0, "dirs": 0, "bytes": 0},
            duration_ms=int(duration_ms),
            **fields,
        )
    )


def pending_approval_result(
    ctx: OperationContext | None = None,
    *,
    kind: OperationKind,
    logical_path: str = "",
    target_path: str = "",
    old_revision: str = "",
    plan: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    duration_ms: int = 0,
    **fields: Any,
) -> OperationResult:
    """已准备就绪、等待审批（``error`` 留空：这不是错误）。"""
    return OperationResult(
        **_from_context(
            ctx,
            kind=kind,
            status=OperationStatus.PENDING_APPROVAL,
            logical_path=logical_path,
            target_path=target_path,
            old_revision=old_revision,
            approval_state=OperationApprovalState.PENDING,
            rollback_state=RollbackState.NOT_APPLICABLE,
            stats=dict(plan or {}),
            warnings=list(warnings or []),
            duration_ms=int(duration_ms),
            **fields,
        )
    )


def denied_result(
    ctx: OperationContext | None = None,
    *,
    kind: OperationKind,
    logical_path: str = "",
    code: str = "DENIED_BY_POLICY",
    message: str = "",
    details: dict[str, Any] | None = None,
    duration_ms: int = 0,
    **fields: Any,
) -> OperationResult:
    approved = OperationApprovalState.REJECTED if str(code) == "DENIED_BY_USER" else (
        OperationApprovalState.NOT_REQUIRED
    )
    return OperationResult(
        **_from_context(
            ctx,
            kind=kind,
            status=OperationStatus.DENIED,
            logical_path=logical_path,
            approval_state=approved,
            rollback_state=RollbackState.NOT_APPLICABLE,
            error=operation_error(code, message, details=details),
            duration_ms=int(duration_ms),
            **fields,
        )
    )


def failed_result(
    ctx: OperationContext | None = None,
    *,
    kind: OperationKind,
    logical_path: str = "",
    code: str = "FAILED",
    message: str = "",
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    duration_ms: int = 0,
    **fields: Any,
) -> OperationResult:
    return OperationResult(
        **_from_context(
            ctx,
            kind=kind,
            status=OperationStatus.FAILED,
            logical_path=logical_path,
            error=operation_error(
                code, message, details=details, retryable=retryable
            ),
            duration_ms=int(duration_ms),
            **fields,
        )
    )


def preview_result(
    ctx: OperationContext | None = None,
    *,
    kind: OperationKind,
    logical_path: str = "",
    target_path: str = "",
    old_revision: str = "",
    plan: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    duration_ms: int = 0,
    **fields: Any,
) -> OperationResult:
    """``dry_run`` 预览：真实状态是"未执行"，由 ``dry_run=True`` 明确标出。

    故意不用 ``success``：没有写任何东西却说"成功"会误导调用方与前端；这里用
    ``no_change`` + ``dry_run`` 表达"目标状态尚未改变，下面是计划"。
    """
    return OperationResult(
        **_from_context(
            ctx,
            kind=kind,
            status=OperationStatus.NO_CHANGE,
            logical_path=logical_path,
            target_path=target_path,
            old_revision=old_revision,
            new_revision=old_revision,
            dry_run=True,
            rollback_state=RollbackState.NOT_NEEDED,
            stats=dict(plan or {}),
            warnings=[*(warnings or []), "dry_run：未写入/未移动/未删除任何内容"],
            duration_ms=int(duration_ms),
            **fields,
        )
    )


def apply_to_tool_output(tool_output: Any, result: OperationResult, *, tool_name: str = "") -> Any:
    """把 ``OperationResult`` 就地折进既有 ``ToolOutput``（幂等，可重复调用）。"""
    projected = result.to_tool_output(tool_name=tool_name)
    for field in ("status", "data", "content_type", "output", "error", "error_code", "retryable"):
        setattr(tool_output, field, getattr(projected, field))
    tool_output.metadata = {**getattr(tool_output, "metadata", {}), **projected.metadata}
    tool_output.meta = projected.meta
    return tool_output


def elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - float(started_at)) * 1000))


__all__ = [
    "OperationResult",
    "already_absent_result",
    "apply_to_tool_output",
    "denied_result",
    "elapsed_ms",
    "failed_result",
    "no_change_result",
    "pending_approval_result",
    "preview_result",
    "success_result",
]
