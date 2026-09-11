"""路由契约：``TaskProfile``（任务画像，只描述事实）。

链路：``TaskProfile`` → ``RouteDecision``（route_decision.py）→
``ExecutionRequest``（execution_request.py）。

为什么要独立：Skill **不应该依赖 RouterDecision**，否则业务实现与路由实现耦合。
本模块只放画像；决策与执行请求各自独立成模块，旧导入路径
（``lumi_contracts.routing.task_profile``）继续可用。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class InfoSource(StrEnum):
    """信息来自哪里（决定要不要读取、读哪一类）。"""

    USER_PROVIDED = "USER_PROVIDED"
    CONVERSATION_MEMORY = "CONVERSATION_MEMORY"
    INTERNAL_KNOWLEDGE = "INTERNAL_KNOWLEDGE"
    WORKSPACE = "WORKSPACE"
    ATTACHED_FILE = "ATTACHED_FILE"
    PUBLIC_WEB = "PUBLIC_WEB"
    PRIVATE_SERVICE = "PRIVATE_SERVICE"
    SYSTEM_STATE = "SYSTEM_STATE"


class Complexity(StrEnum):
    ATOMIC = "ATOMIC"
    SEQUENTIAL = "SEQUENTIAL"
    DYNAMIC = "DYNAMIC"


class ExecutionTarget(StrEnum):
    SERVER = "SERVER"
    DESKTOP = "DESKTOP"
    SANDBOX = "SANDBOX"
    NONE = "NONE"


class TaskProfile(BaseModel):
    """任务画像：只描述"事实"，不含工具名与执行细节。"""

    goal: str = ""
    complexity: Complexity = Complexity.ATOMIC
    side_effects: bool = False
    info_sources: list[InfoSource] = Field(default_factory=lambda: [InfoSource.USER_PROVIDED])
    output_target: str = ""
    execution_target: ExecutionTarget = ExecutionTarget.NONE
    # 抽象能力（如 DOCUMENT_READ / CODE_EXECUTION），**不是工具名**。
    required_capabilities: list[str] = Field(default_factory=list)
    risk_level: str = "low"
    confidence: float = 0.0
    # 原始评估审计字段（不含用户原文）。
    debug: dict = Field(default_factory=dict)

    @property
    def needs_workspace(self) -> bool:
        return InfoSource.WORKSPACE in set(self.info_sources)


__all__ = [
    "Complexity",
    "ExecutionTarget",
    "InfoSource",
    "TaskProfile",
]


