"""业务无关的任务画像契约（goal / sources / complexity / safety）。

The application planner emits this contract; the orchestration kernel uses it
for policy decisions (entry routing, execution policy, plan-first gating)
without knowing any business vocabulary.  Application-specific skill matching
and abstract-node compilation stay in the host application.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Goal = Literal["ANSWER", "GENERATE", "RETRIEVE", "ANALYZE", "EXECUTE", "INTERACT"]
Source = Literal[
    "USER_INPUT", "ATTACHED_FILE", "WORKSPACE_READ", "LOCAL_KNOWLEDGE", "PUBLIC_WEB",
    "EXTERNAL_API", "SYSTEM_STATE",
]
Complexity = Literal["ATOMIC", "SEQUENTIAL", "DYNAMIC"]
Safety = Literal["READ_ONLY", "SAFE_WRITE", "RISKY_WRITE", "CRITICAL"]

SAFETY_ORDER = {"READ_ONLY": 1, "SAFE_WRITE": 2, "RISKY_WRITE": 3, "CRITICAL": 4}


class TaskProfile(BaseModel):
    """与业务词解耦的任务能力需求契约。"""

    goal: Goal
    required_sources: list[Source] = Field(default_factory=lambda: ["USER_INPUT"])
    complexity: Complexity = "ATOMIC"
    safety_level: Safety = "READ_ONLY"
    has_side_effect: bool = False
    needs_runtime_decision: bool = False
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    entities: dict = Field(default_factory=dict)


class AbstractTaskNode(BaseModel):
    """画像层的抽象步骤：由应用编译为可执行节点或 Skill。"""

    id: str
    name: str
    profile: TaskProfile
    depends_on: list[str] = Field(default_factory=list)
    is_critical: bool = True
    instruction: str = ""


__all__ = [
    "AbstractTaskNode",
    "Complexity",
    "Goal",
    "SAFETY_ORDER",
    "Safety",
    "Source",
    "TaskProfile",
]
