"""统一工作区操作契约（``app.contracts.operations``）。

    Provider 原始结果
      → OperationResult          （本包：状态 / 版本 / 变化 / 审批 / 审计标识）
      → ToolOutput 兼容投影      （``OperationResult.to_tool_output``，唯一执行信封）
      → Model / UI / Audit 投影  （既有 tool_output_pipeline）

四个工具（``workspace.write@1`` / ``workspace.edit@1`` / ``workspace.move@1`` /
``workspace.delete@1``）共用这里的 :class:`OperationContext` / :class:`OperationResult` /
:class:`OperationError`；上下文**只能由服务端注入**（见 ``OperationContext`` 文档）。
"""

from __future__ import annotations

from app.contracts.operations.common import (
    TRASH_DIRNAME,
    TRASH_INDEX_NAME,
    ChangeSummary,
    OperationApprovalState,
    OperationContext,
    OperationKind,
    OperationLimits,
    OperationStatus,
    ProtectionPolicy,
    RevisionRules,
    RollbackState,
    TrashPolicy,
    is_trash_path,
    is_within,
    normalize_rel_path,
    parent_of,
    trash_entry_id,
    trash_entry_path,
)
from app.contracts.operations.delete import (
    DeleteChange,
    DeleteRequest,
    DeleteStats,
    TrashRecord,
)
from app.contracts.operations.edit import EditChange, EditRequest
from app.contracts.operations.errors import (
    CAPABILITY_CODE_BY_OPERATION_ERROR,
    OperationError,
    OperationErrorCode,
    OperationErrorSpec,
    error_spec,
    operation_error,
    registered_codes,
)
from app.contracts.operations.move import MoveChange, MoveRequest
from app.contracts.operations.results import (
    OperationResult,
    already_absent_result,
    apply_to_tool_output,
    denied_result,
    elapsed_ms,
    failed_result,
    no_change_result,
    pending_approval_result,
    preview_result,
    success_result,
)
from app.contracts.operations.write import (
    EXECUTABLE_SUFFIXES,
    NEWLINE_POLICIES,
    WriteChange,
    WriteRequest,
)

__all__ = [
    "CAPABILITY_CODE_BY_OPERATION_ERROR",
    "ChangeSummary",
    "DeleteChange",
    "DeleteRequest",
    "DeleteStats",
    "EXECUTABLE_SUFFIXES",
    "EditChange",
    "EditRequest",
    "MoveChange",
    "MoveRequest",
    "NEWLINE_POLICIES",
    "OperationApprovalState",
    "OperationContext",
    "OperationError",
    "OperationErrorCode",
    "OperationErrorSpec",
    "OperationKind",
    "OperationLimits",
    "OperationResult",
    "OperationStatus",
    "ProtectionPolicy",
    "RevisionRules",
    "RollbackState",
    "TRASH_DIRNAME",
    "TRASH_INDEX_NAME",
    "TrashPolicy",
    "TrashRecord",
    "WriteChange",
    "WriteRequest",
    "already_absent_result",
    "apply_to_tool_output",
    "denied_result",
    "elapsed_ms",
    "error_spec",
    "failed_result",
    "is_trash_path",
    "is_within",
    "no_change_result",
    "normalize_rel_path",
    "operation_error",
    "parent_of",
    "pending_approval_result",
    "preview_result",
    "registered_codes",
    "success_result",
    "trash_entry_id",
    "trash_entry_path",
]
