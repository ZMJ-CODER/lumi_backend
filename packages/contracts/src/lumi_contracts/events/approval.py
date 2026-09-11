"""审批契约：审批请求/结果与指纹绑定。

关键约束：审批必须绑定**确切调用参数**（指纹），否则"用户批准了 A、实际执行了 B"。
指纹由服务端计算；插件与模型不得自填。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalScope(StrEnum):
    CALL = "call"
    TASK = "task"
    WORKSPACE = "workspace"


class ApprovalState(BaseModel):
    """一次审批的完整状态（可持久化、可恢复）。"""

    call_id: str = ""
    job_id: str = ""
    tool_name: str = ""
    scope: ApprovalScope = ApprovalScope.CALL
    decision: ApprovalDecision = ApprovalDecision.PENDING
    risk: str = ""
    reason: str = ""
    # 绑定的参数指纹：恢复/重放时用于校验"批准的还是这次调用"。
    fingerprint: str = ""
    requested_at: float = 0.0
    resolved_at: float = 0.0
    expires_at: float = 0.0
    resolved_by: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_pending(self) -> bool:
        return self.decision is ApprovalDecision.PENDING


def approval_fingerprint(
    tool_name: str,
    arguments: dict[str, Any] | None,
    context_hash: str = "",
) -> str:
    """计算审批指纹：工具名 + 规范化参数 + 上下文哈希。

    参数按 key 排序并忽略执行期保留字段（``_lumi_execution_policy``），保证同一
    语义调用得到同一指纹、不同参数得到不同指纹。
    """
    payload = {
        key: value
        for key, value in dict(arguments or {}).items()
        if not str(key).startswith("_lumi_")
    }
    encoded = json.dumps(
        {"tool": str(tool_name or ""), "args": payload, "ctx": str(context_hash or "")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ApprovalDecision",
    "ApprovalScope",
    "ApprovalState",
    "approval_fingerprint",
]
