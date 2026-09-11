"""``ExecutionRequest``：一次执行的请求契约。

链路：``TaskProfile → RouteDecision → ExecutionRequest → SkillRequest/ToolRequest``。
请求里只放"服务端钳制过的执行约束"：指令、服务端上下文、路由、允许/禁止的工具、
步数上限、数据敏感级与幂等/审批绑定；身份字段一律来自 ``ServerContext``。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from lumi_contracts.common.context import Sensitivity, ServerContext
from lumi_contracts.routing.route_decision import RouteDecision


class ExecutionRequest(BaseModel):
    """一次执行的请求：服务端上下文 + 指令 + 能力约束。"""

    instruction: str = ""
    context: ServerContext = Field(default_factory=ServerContext)
    route: RouteDecision | None = None
    # 允许使用的能力/工具（服务端钳制；执行层只能收窄不能扩张）。
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    max_steps: int = 1
    max_chars: int = 0
    data_sensitivity: Sensitivity = Sensitivity.INTERNAL
    # 幂等/审批绑定（服务端生成）。
    idempotency_key: str = ""
    approval_fingerprint: str = ""


__all__ = ["ExecutionRequest"]
