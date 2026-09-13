"""``RouteDecision``：路由决策契约（只描述"选了什么"）。

与 ``TaskProfile`` 分开：画像只描述事实，决策只描述选择。决策里不携带任何工具
实现，执行层按 ``required_capabilities`` 解析成具体工具。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from lumi_contracts.routing.capability_preflight import CapabilityPreflight
from lumi_contracts.routing.task_profile import TaskProfile


class RouteMode(StrEnum):
    DIRECT_CHAT = "direct_chat"
    ATOMIC_READ = "m1_atomic_read"
    SINGLE_ACTION = "single_action_skill"
    PLANNER_DAG = "planner_dag"
    REACT = "react"
    BLOCKED = "blocked"


class RouteModeV2(StrEnum):
    """方案 4 §2.1 的执行模式（路由唯一输出）。

    与既有 ``RouteMode`` **并存而不替换**：``RouteMode`` 是历史契约视图（``react`` /
    ``single_action_skill`` 等旧名），``RouteModeV2`` 是内核 ``ExecutionMode`` 的同名
    词表。旧字段继续可读，新字段是加法，避免破坏既有前端/验收日志。
    """

    DIRECT_CHAT = "direct_chat"
    M1_ATOMIC_READ = "m1_atomic_read"
    M1_ATOMIC_ACTION = "m1_atomic_action"
    SEQUENTIAL_WORKFLOW = "sequential_workflow"
    DYNAMIC_AGENT = "dynamic_agent"


#: 内核对齐用的模式字符串全集（``lumi_orch.execution_router.ExecutionMode`` 的取值）；
#: 由 ``tests/test_task_profile_alignment.py`` 逐值比对，防止两侧漂移。
EXECUTION_MODE_VALUES: tuple[str, ...] = tuple(item.value for item in RouteModeV2)


class RouteDecision(BaseModel):
    """路由决策：只描述"选了什么"，不携带工具实现。

    方案 4 §2.3 的 schema v2 字段全部是**加法**（有默认值），旧读者不受影响。
    """

    mode: RouteMode = RouteMode.DIRECT_CHAT
    reason: str = ""
    profile: TaskProfile | None = None
    # 决策依据（可审计的短标签，不放用户原文）。
    signals: dict = Field(default_factory=dict)
    # 需要的能力（抽象名），由执行层解析成具体工具。
    required_capabilities: list[str] = Field(default_factory=list)
    blocked_reason: str = ""

    # ── 方案 4 §2.3：schema v2 新增（加法）──────────────────────
    #: 契约 schema 版本（v2 起含本类的新字段；旧快照没有该字段 = v1）。
    schema_version: int = 2
    #: 执行模式（内核同名词表）；``mode`` 保留为历史契约视图。
    route_mode: str = ""
    intent_type: str = ""
    action_intents: list[str] = Field(default_factory=list)
    target_scope: str = ""
    target_clarity: str = ""
    approval_required: bool = False
    #: 目标未知时进入澄清（不是失败）。
    needs_clarification: bool = False
    confidence: float = 0.0
    confidence_source: str = ""
    #: 稳定原因码（自由文本不得用于业务判断）。
    decision_reason_code: str = ""
    #: 预检结论（方案 §3.3；``None`` = 尚未预检）。
    capability_preflight: "CapabilityPreflight | None" = None

    @property
    def suspended(self) -> bool:
        """需要人工介入（澄清/审批）——不允许"继续自动执行"。"""
        return bool(self.needs_clarification or self.approval_required)


#: 契约 ``mode``（历史视图）→ 内核执行模式串（唯一映射表）。
LEGACY_CONTRACT_MODE_TO_ROUTE_MODE: dict[str, str] = {
    RouteMode.DIRECT_CHAT.value: RouteModeV2.DIRECT_CHAT.value,
    RouteMode.ATOMIC_READ.value: RouteModeV2.M1_ATOMIC_READ.value,
    RouteMode.SINGLE_ACTION.value: RouteModeV2.M1_ATOMIC_ACTION.value,
    RouteMode.PLANNER_DAG.value: RouteModeV2.SEQUENTIAL_WORKFLOW.value,
    RouteMode.REACT.value: RouteModeV2.DYNAMIC_AGENT.value,
    RouteMode.BLOCKED.value: "",
}


__all__ = [
    "EXECUTION_MODE_VALUES",
    "LEGACY_CONTRACT_MODE_TO_ROUTE_MODE",
    "RouteDecision",
    "RouteMode",
    "RouteModeV2",
]
