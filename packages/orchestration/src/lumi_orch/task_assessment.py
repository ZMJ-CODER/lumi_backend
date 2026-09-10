"""严格任务画像契约（单一事实来源）。

与前端/Assessor 共享的类型定义：任何数组字段都不允许单值写法，
``output_target`` / ``execution_target`` 为严格单值；``side_effects`` 非空
时禁止 M0 与 M1_ATOMIC_READ（由 ``lumi_orch.execution_router`` 强制）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Complexity = Literal["M0", "M1", "M2", "M3"]
IntentType = Literal["GENERATE_ONLY", "EXECUTE_ACTION"]
SideEffect = Literal["WRITE", "DELETE", "SEND", "EXECUTE", "PUBLISH"]
InfoSource = Literal[
    "INTERNAL_KNOWLEDGE",
    "USER_PROVIDED",
    "CONVERSATION_MEMORY",
    "WORKSPACE",
    "EXTERNAL_WEB",
    "PRIVATE_SERVICE",
]
OutputTarget = Literal[
    "CHAT", "WORKSPACE", "DOWNLOAD_ARTIFACT", "EXTERNAL_SERVICE", "LOCAL_APPLICATION",
]
ExecutionTarget = Literal["NONE", "BACKEND", "DESKTOP", "SANDBOX", "EXTERNAL_SERVICE"]
PathDeterminism = Literal["KNOWN", "UNKNOWN"]
RiskLevel = Literal["READ_ONLY", "REVERSIBLE", "REQUIRES_APPROVAL", "HIGH_RISK"]
DataSensitivity = Literal["NORMAL", "PRIVATE", "CREDENTIAL", "PII"]
ContextSizeEstimate = Literal["SMALL", "MEDIUM", "LARGE"]

# 已在上下文中的来源：无需任何外部调用即可直接回答。
CONTEXT_READY_SOURCES = frozenset({"USER_PROVIDED", "CONVERSATION_MEMORY", "INTERNAL_KNOWLEDGE"})
EXTERNAL_READ_SOURCES = frozenset({"WORKSPACE", "EXTERNAL_WEB", "PRIVATE_SERVICE"})
DESKTOP_DEPENDENT_TARGETS = frozenset({"DESKTOP"})
HIGH_RISK_SIDE_EFFECTS = frozenset({"DELETE", "EXECUTE"})

_COMPLEXITY_ORDER = {"M0": 0, "M1": 1, "M2": 2, "M3": 3}
_RISK_ORDER = {"READ_ONLY": 0, "REVERSIBLE": 1, "REQUIRES_APPROVAL": 2, "HIGH_RISK": 3}


class TaskProfile(BaseModel):
    """核心数据模型：任务画像（严格 schema，禁止未知字段）。"""

    model_config = ConfigDict(extra="forbid")

    complexity: Complexity
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    intent_type: IntentType = "GENERATE_ONLY"
    side_effects: list[SideEffect] = Field(default_factory=list)
    info_sources: list[InfoSource] = Field(default_factory=lambda: ["USER_PROVIDED"])
    output_target: OutputTarget = "CHAT"
    execution_target: ExecutionTarget = "NONE"
    required_capabilities: list[str] = Field(default_factory=list)
    path_determinism: PathDeterminism = "KNOWN"
    estimated_steps: int | None = Field(default=None, ge=1)
    risk_level: RiskLevel = "READ_ONLY"
    data_sensitivity: DataSensitivity = "NORMAL"
    context_size_estimate: ContextSizeEstimate = "SMALL"


def has_side_effects(profile: TaskProfile) -> bool:
    return bool(profile.side_effects)


def needs_external_read(profile: TaskProfile) -> bool:
    return any(source in EXTERNAL_READ_SOURCES for source in profile.info_sources)


def is_context_ready(profile: TaskProfile) -> bool:
    return all(source in CONTEXT_READY_SOURCES for source in profile.info_sources)


def max_complexity(left: Complexity, right: Complexity) -> Complexity:
    return left if _COMPLEXITY_ORDER[left] >= _COMPLEXITY_ORDER[right] else right


def apply_confidence_policy(profile: TaskProfile, *, threshold: float = 0.55) -> TaskProfile:
    """低置信度保守降级：只对“有副作用”的任务升级复杂度/风险。

    - 有副作用：complexity 至少 M2、路径视为 UNKNOWN、风险至少 REQUIRES_APPROVAL；
    - 只读/纯生成：保持复杂度不变（保守体现在不冒险执行动作，而不是把
      只读问答升级成编排 —— 否则会出现“只显示计划、没有正文”的空答复）。
    """
    if profile.confidence >= threshold:
        return profile
    if not has_side_effects(profile):
        return profile
    updates: dict = {"complexity": max_complexity(profile.complexity, "M2")}
    updates["path_determinism"] = "UNKNOWN"
    if _RISK_ORDER[profile.risk_level] < _RISK_ORDER["REQUIRES_APPROVAL"]:
        updates["risk_level"] = "REQUIRES_APPROVAL"
    return profile.model_copy(update=updates)


__all__ = [
    "CONTEXT_READY_SOURCES",
    "EXTERNAL_READ_SOURCES",
    "Complexity",
    "ContextSizeEstimate",
    "DataSensitivity",
    "ExecutionTarget",
    "HIGH_RISK_SIDE_EFFECTS",
    "InfoSource",
    "IntentType",
    "OutputTarget",
    "PathDeterminism",
    "RiskLevel",
    "SideEffect",
    "TaskProfile",
    "apply_confidence_policy",
    "has_side_effects",
    "is_context_ready",
    "max_complexity",
    "needs_external_read",
]
