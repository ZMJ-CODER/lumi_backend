"""``RouteDecision``：路由决策契约（只描述"选了什么"）。

与 ``TaskProfile`` 分开：画像只描述事实，决策只描述选择。决策里不携带任何工具
实现，执行层按 ``required_capabilities`` 解析成具体工具。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from lumi_contracts.routing.task_profile import TaskProfile


class RouteMode(StrEnum):
    DIRECT_CHAT = "direct_chat"
    ATOMIC_READ = "m1_atomic_read"
    SINGLE_ACTION = "single_action_skill"
    PLANNER_DAG = "planner_dag"
    REACT = "react"
    BLOCKED = "blocked"


class RouteDecision(BaseModel):
    """路由决策：只描述"选了什么"，不携带工具实现。"""

    mode: RouteMode = RouteMode.DIRECT_CHAT
    reason: str = ""
    profile: TaskProfile | None = None
    # 决策依据（可审计的短标签，不放用户原文）。
    signals: dict = Field(default_factory=dict)
    # 需要的能力（抽象名），由执行层解析成具体工具。
    required_capabilities: list[str] = Field(default_factory=list)
    blocked_reason: str = ""


__all__ = ["RouteDecision", "RouteMode"]
