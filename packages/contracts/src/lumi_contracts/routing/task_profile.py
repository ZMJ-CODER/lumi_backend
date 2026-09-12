"""路由契约：``TaskProfile``（任务画像，只描述事实）。

链路：``TaskProfile`` → ``RouteDecision``（route_decision.py）→
``ExecutionRequest``（execution_request.py）。

为什么要独立：Skill **不应该依赖 RouterDecision**，否则业务实现与路由实现耦合。
本模块只放画像；决策与执行请求各自独立成模块，旧导入路径
（``lumi_contracts.routing.task_profile``）继续可用。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError


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


class IntentType(StrEnum):
    """任务大类（方案 §3.1）：只生成 vs 要动手。"""

    GENERATE_ONLY = "GENERATE_ONLY"
    EXECUTE_ACTION = "EXECUTE_ACTION"


class ActionIntent(StrEnum):
    READ = "READ"
    SEARCH = "SEARCH"
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
    EXECUTE = "EXECUTE"
    SEND = "SEND"
    PUBLISH = "PUBLISH"


class TargetScope(StrEnum):
    WORKSPACE = "WORKSPACE"
    ATTACHMENT = "ATTACHMENT"
    USER_INPUT = "USER_INPUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    SYSTEM_STATE = "SYSTEM_STATE"
    NONE = "NONE"


class TargetClarity(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class ConfidenceSource(StrEnum):
    """置信度来源（稳定枚举）：LLM / 启发式 / 规则纠正。"""

    LLM = "llm"
    HEURISTIC = "heuristic"
    RULE_CORRECTED = "rule_corrected"


#: 旧画像字段名 → 统一字段名（兼容转换用；只做名称归一，不改语义）。
FIELD_ALIASES: dict[str, str] = {
    "intent": "intent_type",
    "intents": "action_intents",
    "actions": "action_intents",
    "action": "action_intents",
    "scope": "target_scope",
    "clarity": "target_clarity",
    "needs_dependency": "has_dependency",
    "needs_runtime_decision": "has_runtime_decision",
    "capabilities": "required_capabilities",
    "confidence_source_kind": "confidence_source",
}


class TaskProfile(BaseModel):
    """任务画像：**唯一权威定义**（方案 §3.1；contracts 之外只保留适配器）。

    原有字段（``goal`` / ``complexity`` / ``info_sources`` …）保持不变，新增的
    §3.1 字段是**加法**，因此旧读者不受影响。
    """

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
    # ── §3.1 统一语义（加法；路由/预检/工具注入的唯一依据）──
    intent_type: IntentType = IntentType.GENERATE_ONLY
    action_intents: list[ActionIntent] = Field(default_factory=list)
    target_scope: TargetScope = TargetScope.NONE
    target_clarity: TargetClarity = TargetClarity.KNOWN
    has_dependency: bool = False
    has_runtime_decision: bool = False
    approval_required: bool = False
    confidence_source: ConfidenceSource = ConfidenceSource.HEURISTIC
    #: 稳定原因码（自由文本不进这里）。
    decision_reason_code: str = ""

    @property
    def needs_workspace(self) -> bool:
        return InfoSource.WORKSPACE in set(self.info_sources)

    @classmethod
    def from_mapping(cls, values: object) -> "TaskProfile":
        """兼容转换器：任意旧画像（dict / 对象）→ 统一画像。

        只做**别名归一 + 未知字段忽略**，不做语义猜测；解析不了的枚举值退回默认，
        并把原因记在 ``debug["coerced_fields"]``（影子比对用）。
        """
        if isinstance(values, cls):
            return values
        source: dict = {}
        if isinstance(values, dict):
            source = {str(k): v for k, v in values.items()}
        else:
            for name in (*cls.model_fields, *FIELD_ALIASES):
                if hasattr(values, name):
                    source[name] = getattr(values, name)  # type: ignore[union-attr]
        normalized: dict = {}
        coerced: dict = {}
        for key, value in source.items():
            target = FIELD_ALIASES.get(key, key)
            if target not in cls.model_fields or target in normalized:
                continue
            field = cls.model_fields[target]
            annotation = str(field.annotation)
            try:
                if "ActionIntent" in annotation and not isinstance(value, ActionIntent):
                    normalized[target] = [
                        ActionIntent(str(item)) for item in (value or [])
                    ] if isinstance(value, (list, tuple, set)) else [ActionIntent(str(value))]
                elif "list" in annotation and isinstance(value, str):
                    normalized[target] = [value] if value else []
                elif target == "debug" and not isinstance(value, dict):
                    continue
                else:
                    normalized[target] = value
            except (ValueError, TypeError):
                coerced[target] = str(value)[:80]
        # 其余字段（枚举单值 / 列表元素）交给 pydantic 校验：解析不了的**逐字段**退回
        # 默认值并留痕，而不是让整个转换抛错——旧画像的 complexity 档位（M0..M3）、
        # 信息源名（EXTERNAL_WEB）在 canonical 词表里都可能是非法值。
        while True:
            if coerced:
                normalized["debug"] = {
                    **(normalized.get("debug") or {}), "coerced_fields": coerced
                }
            try:
                return cls.model_validate(normalized)
            except ValidationError as exc:
                rejected = {
                    str(error.get("loc", ("",))[0])
                    for error in exc.errors()
                    if error.get("loc")
                } & set(normalized)
                if not rejected:
                    raise
                for name in rejected:
                    coerced[name] = str(normalized.get(name))[:80]
                    normalized.pop(name, None)

    def fingerprint(self) -> dict:
        """影子比对用的稳定摘要（不含用户原文）。"""
        return {
            "intent_type": str(self.intent_type),
            "action_intents": sorted(str(item) for item in self.action_intents),
            "target_scope": str(self.target_scope),
            "target_clarity": str(self.target_clarity),
            "complexity": str(self.complexity),
            "required_capabilities": sorted(str(item) for item in self.required_capabilities),
            "risk_level": self.risk_level,
            "has_dependency": bool(self.has_dependency),
            "approval_required": bool(self.approval_required),
            "confidence_source": str(self.confidence_source),
            "decision_reason_code": self.decision_reason_code,
        }


__all__ = [
    "FIELD_ALIASES",
    "ActionIntent",
    "Complexity",
    "ConfidenceSource",
    "ExecutionTarget",
    "InfoSource",
    "IntentType",
    "TargetClarity",
    "TargetScope",
    "TaskProfile",
]


