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

# ── 方案 4：动作意图 / 目标范围与清晰度（与契约 ``lumi_contracts`` 同名同值）──
# 内核不依赖契约包，因此这里用 ``Literal`` 表达同一套词表；两侧由测试逐值比对。
ActionIntent = Literal["READ", "SEARCH", "CREATE", "MODIFY", "DELETE", "EXECUTE", "SEND", "PUBLISH"]
TargetScope = Literal[
    "WORKSPACE", "ATTACHMENT", "USER_INPUT", "EXTERNAL_SERVICE", "SYSTEM_STATE", "NONE",
]
TargetClarity = Literal["KNOWN", "UNKNOWN"]
ConfidenceSource = Literal["llm", "heuristic", "rule_corrected"]

#: 已在上下文中的来源：无需任何外部调用即可直接回答。
CONTEXT_READY_SOURCES = frozenset({"USER_PROVIDED", "CONVERSATION_MEMORY", "INTERNAL_KNOWLEDGE"})
EXTERNAL_READ_SOURCES = frozenset({"WORKSPACE", "EXTERNAL_WEB", "PRIVATE_SERVICE"})
DESKTOP_DEPENDENT_TARGETS = frozenset({"DESKTOP"})
#: 必须审批的副作用（方案 4 §2.2：DELETE / EXECUTE / PUBLISH 不因高置信度自动执行）。
HIGH_RISK_SIDE_EFFECTS = frozenset({"DELETE", "EXECUTE", "PUBLISH"})
#: 只读动作意图（其余都是"动手"，非空即禁止直聊/原子读）。
READ_ONLY_ACTIONS = frozenset({"READ", "SEARCH"})

#: 副作用 → 动作意图的**唯一**兼容映射（旧画像只有副作用词表时用）。
SIDE_EFFECT_TO_ACTIONS: dict[str, tuple[str, ...]] = {
    "WRITE": ("MODIFY",),
    "DELETE": ("DELETE",),
    "SEND": ("SEND",),
    "EXECUTE": ("EXECUTE",),
    "PUBLISH": ("PUBLISH",),
}

_COMPLEXITY_ORDER = {"M0": 0, "M1": 1, "M2": 2, "M3": 3}
_RISK_ORDER = {"READ_ONLY": 0, "REVERSIBLE": 1, "REQUIRES_APPROVAL": 2, "HIGH_RISK": 3}


class TaskProfile(BaseModel):
    """核心数据模型：任务画像（严格 schema，禁止未知字段）。

    方案 4 新增字段全部**有默认值**（加法），旧构造点与旧快照解析不受影响。
    """

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
    # ── 方案 4 §1.1：统一语义（路由/预检/工具注入的唯一依据）──
    action_intents: list[ActionIntent] = Field(default_factory=list)
    target_scope: TargetScope = "NONE"
    target_clarity: TargetClarity = "KNOWN"
    has_dependency: bool = False
    has_runtime_decision: bool = False
    approval_required: bool = False
    confidence_source: ConfidenceSource = "heuristic"
    #: 稳定原因码（自由文本不得进这里做业务判断）。
    decision_reason_code: str = ""


def has_side_effects(profile: TaskProfile) -> bool:
    return bool(profile.side_effects)


def effective_action_intents(profile: TaskProfile) -> tuple[str, ...]:
    """画像的**有效**动作意图（方案 4 §2.2 硬约束的唯一读法）。

    * 画像显式给了 ``action_intents`` → 原样使用（新画像优先）；
    * 只给了旧 ``side_effects`` → 按唯一映射表推导（兼容旧画像，不猜别的）；
    * 需要工作区但只有读意图 → 补 ``READ``（工作区依赖本身就是读动作）。

    绝不返回"猜出来"的写意图：推导只来自 ``side_effects`` 这一份事实。
    """
    intents: list[str] = [str(item) for item in (profile.action_intents or []) if str(item)]
    if not intents:
        for effect in profile.side_effects or []:
            for action in SIDE_EFFECT_TO_ACTIONS.get(str(effect), ()):
                if action not in intents:
                    intents.append(action)
    if "WORKSPACE" in (profile.info_sources or []) and not intents:
        intents.append("READ")
    return tuple(intents)


def has_action_intents(profile: TaskProfile) -> bool:
    """是否有动作意图（非空 ⇒ 禁止 DIRECT_CHAT 与原子只读）。"""
    return bool(effective_action_intents(profile))


def requires_approval(profile: TaskProfile) -> bool:
    """是否需要人工审批：显式声明、高风险副作用、或画像自身要求。

    ``DELETE`` / ``EXECUTE`` / ``PUBLISH`` 一律需要审批或安全阻断（方案 4 §2.2），
    与置信度无关——"分类器很自信"不是自动执行高风险操作的依据。

    判定同时看 **动作意图** 与旧 ``side_effects``：只给动作意图（新画像）的画像也必须
    被拦住，否则"新画像判写入/删除、旧词表没有副作用信号"就成了审批漏点。
    """
    if bool(profile.approval_required):
        return True
    if profile.risk_level in {"REQUIRES_APPROVAL", "HIGH_RISK"}:
        return True
    if set(profile.side_effects or []) & HIGH_RISK_SIDE_EFFECTS:
        return True
    return bool(set(effective_action_intents(profile)) & HIGH_RISK_SIDE_EFFECTS)


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
    "READ_ONLY_ACTIONS",
    "SIDE_EFFECT_TO_ACTIONS",
    "ActionIntent",
    "Complexity",
    "ConfidenceSource",
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
    "TargetClarity",
    "TargetScope",
    "TaskProfile",
    "apply_confidence_policy",
    "effective_action_intents",
    "has_action_intents",
    "has_side_effects",
    "is_context_ready",
    "max_complexity",
    "needs_external_read",
    "requires_approval",
]
