"""动态策略引擎：在既有能力候选与标准失败类别之间做受限裁决。

策略不是意图识别器：它绝不读取用户原始文本、业务关键词、工具名称或
任意 Python 表达式。Planner 先产出 ``TaskProfile``，Dispatcher 先找出
符合画像的 Skill 候选；本模块只在这些已验证的候选之间进行选择，并为
失败恢复、澄清节奏提供可热加载的声明式决策。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


GoalName = Literal["GENERATE", "RETRIEVE", "ANALYZE", "EXECUTE", "INTERACT"]
ComplexityName = Literal["ATOMIC", "SEQUENTIAL", "DYNAMIC"]
SafetyName = Literal["READ_ONLY", "SAFE_WRITE", "RISKY_WRITE", "CRITICAL"]
SelectionMode = Literal["balanced", "economy", "quality"]
FailureAction = Literal[
    "retry", "alternative_or_replan", "replan", "user_action", "abort",
]

_SAFETY_ORDER = {"READ_ONLY": 1, "SAFE_WRITE": 2, "RISKY_WRITE": 3, "CRITICAL": 4}
_FAILURE_CATEGORIES = {
    "input", "permission", "capability_unavailable", "transient",
    "model_action_required", "execution",
}


class Applicability(BaseModel):
    """仅允许用抽象任务画像限定策略的生效范围。"""

    goals: list[GoalName] = Field(default_factory=list, max_length=5)
    complexities: list[ComplexityName] = Field(default_factory=list, max_length=3)
    safety_levels: list[SafetyName] = Field(default_factory=list, max_length=4)
    model_config = {"extra": "forbid"}

    @field_validator("goals", "complexities", "safety_levels")
    @classmethod
    def unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("策略适用条件不能重复")
        return values

    def matches(self, profile: Any) -> bool:
        if self.goals and str(getattr(profile, "goal", "")) not in self.goals:
            return False
        if self.complexities and str(getattr(profile, "complexity", "")) not in self.complexities:
            return False
        return not self.safety_levels or str(getattr(profile, "safety_level", "")) in self.safety_levels


class SelectionWeights(BaseModel):
    """候选排序权重；不接受任何运行时代码或名称匹配规则。"""

    capability_specificity: float = Field(default=1.0, ge=0, le=100)
    declared_capability_bonus: float = Field(default=10.0, ge=0, le=100)
    reliability: float = Field(default=4.0, ge=0, le=100)
    cost: float = Field(default=1.0, ge=0, le=100)
    model_config = {"extra": "forbid"}


class SelectionPolicy(BaseModel):
    mode: SelectionMode = "balanced"
    weights: SelectionWeights = Field(default_factory=SelectionWeights)
    model_config = {"extra": "forbid"}


class InteractionPolicy(BaseModel):
    """澄清只由置信度与风险边界触发，不能依据业务词触发。"""

    confidence_below: float = Field(default=0.4, ge=0, le=1)
    safety_at_or_above: SafetyName = "RISKY_WRITE"
    require_no_prior_context: bool = True
    model_config = {"extra": "forbid"}


class FailurePolicy(BaseModel):
    actions: dict[str, FailureAction] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}

    @field_validator("actions")
    @classmethod
    def known_categories(cls, values: dict[str, FailureAction]) -> dict[str, FailureAction]:
        unknown = set(values) - _FAILURE_CATEGORIES
        if unknown:
            raise ValueError("failure.actions 包含未知错误类别: " + ", ".join(sorted(unknown)))
        return values


class StrategyPolicyDocument(BaseModel):
    """一个 YAML 文件对应一条可装卸策略，且只包含受限数据。"""

    version: Literal[1]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    priority: int = Field(default=100, ge=0, le=10_000)
    enabled: bool = True
    applies_to: Applicability = Field(default_factory=Applicability)
    selection: SelectionPolicy | None = None
    interaction: InteractionPolicy | None = None
    failure: FailurePolicy | None = None
    model_config = {"extra": "forbid"}

@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    """由 Dispatcher 构造的安全候选快照，不含实例路径或用户原文。"""

    value: Any
    name: str
    capability_specificity: float
    declared_capability: bool
    reliability: float | None = None
    cost: float = 1.0


@dataclass(frozen=True, slots=True)
class StrategySelection:
    candidate: StrategyCandidate
    policy_id: str
    score: float
    mode: SelectionMode


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    policies: tuple[StrategyPolicyDocument, ...]
    disabled_ids: frozenset[str]
    version: str
    fallback_active: bool


_SAFE_FALLBACK = StrategyPolicyDocument.model_validate({
    "version": 1,
    "id": "builtin-safe-fallback",
    "priority": 0,
    "enabled": True,
    "selection": {
        "mode": "balanced",
        "weights": {
            "capability_specificity": 1.0,
            "declared_capability_bonus": 10.0,
            "reliability": 4.0,
            "cost": 1.0,
        },
    },
    "interaction": {
        "confidence_below": 0.4,
        "safety_at_or_above": "RISKY_WRITE",
        "require_no_prior_context": True,
    },
    "failure": {
        "actions": {
            "transient": "retry",
            "capability_unavailable": "alternative_or_replan",
            "execution": "alternative_or_replan",
            "input": "replan",
            "permission": "user_action",
            "model_action_required": "user_action",
        },
    },
})


class StrategyEngine:
    """管理策略文件的装载状态，并为每个调用产出不可变快照。

    Redis 仅保存“已卸载策略 ID”；文件仍是策略定义的唯一来源。Redis 不可用
    时保留进程内卸载状态，并始终回落至 ``_SAFE_FALLBACK``，避免配置服务
    故障扩大为办公任务故障。
    """

    _DISABLED_KEY = "config:strategy_engine:disabled"

    def __init__(self, policy_dir: str | Path | None = None) -> None:
        if policy_dir is None:
            from app.core.config import settings

            policy_dir = settings.AGENT_STRATEGY_POLICY_DIR
        self._policy_dir = Path(policy_dir)
        self._local_disabled: set[str] = set()
        self._snapshot: StrategySnapshot | None = None
        self._fingerprint = ""

    @property
    def policy_dir(self) -> Path:
        return self._policy_dir

    def _read_documents(self) -> tuple[list[StrategyPolicyDocument], list[dict[str, str]], str]:
        documents: list[StrategyPolicyDocument] = []
        errors: list[dict[str, str]] = []
        fingerprints: list[str] = []
        if self._policy_dir.exists():
            for path in sorted((*self._policy_dir.glob("*.yaml"), *self._policy_dir.glob("*.yml"))):
                try:
                    raw = path.read_text(encoding="utf-8")
                    document = StrategyPolicyDocument.model_validate(yaml.safe_load(raw) or {})
                    documents.append(document)
                    fingerprints.append(path.name + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest())
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    errors.append({"file": path.name, "error": str(exc)[:300]})
        documents.sort(key=lambda item: (-item.priority, item.id))
        duplicates = {item.id for item in documents if sum(candidate.id == item.id for candidate in documents) > 1}
        if duplicates:
            documents = [item for item in documents if item.id not in duplicates]
            errors.append({"file": "<directory>", "error": "策略 id 重复: " + ", ".join(sorted(duplicates))})
        return documents, errors, hashlib.sha256("|".join(fingerprints).encode("utf-8")).hexdigest()

    async def _stored_disabled_ids(self) -> set[str]:
        try:
            from app.core.redis import get_redis

            values = await get_redis().smembers(self._DISABLED_KEY)
            return {str(value) for value in values}
        except Exception:  # configuration control must fail safe
            return set()

    async def snapshot(self, *, force: bool = False) -> StrategySnapshot:
        documents, _errors, file_fingerprint = self._read_documents()
        disabled = self._local_disabled | await self._stored_disabled_ids()
        fingerprint = file_fingerprint + ":" + ",".join(sorted(disabled))
        if not force and self._snapshot is not None and fingerprint == self._fingerprint:
            return self._snapshot
        active = tuple(item for item in documents if item.enabled and item.id not in disabled)
        payload = {
            "policies": [item.model_dump(mode="json") for item in active],
            "disabled": sorted(disabled),
        }
        version = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._snapshot = StrategySnapshot(
            policies=active,
            disabled_ids=frozenset(disabled),
            version=version,
            fallback_active=not bool(active),
        )
        self._fingerprint = fingerprint
        return self._snapshot

    def current_snapshot(self) -> StrategySnapshot:
        """Return the latest immutable snapshot without I/O.

        Node failure callbacks are intentionally synchronous in the execution
        kernel. Admin load/unload/reload and planning refresh this snapshot
        first; a cold process still gets the built-in safe policy.
        """
        return self._snapshot or StrategySnapshot(
            policies=(), disabled_ids=frozenset(), version="safe-fallback", fallback_active=True
        )

    @staticmethod
    def snapshot_payload(snapshot: StrategySnapshot) -> dict[str, Any]:
        """Persist a small, validated decision snapshot with a planned node."""
        return {
            "version": snapshot.version,
            "policies": [item.model_dump(mode="json") for item in snapshot.policies],
        }

    @staticmethod
    def snapshot_from_payload(value: Any) -> StrategySnapshot | None:
        """Restore a persisted snapshot; malformed legacy metadata is ignored."""
        if not isinstance(value, dict) or not isinstance(value.get("policies"), list):
            return None
        try:
            policies = tuple(StrategyPolicyDocument.model_validate(item) for item in value["policies"])
        except Exception:  # noqa: BLE001
            return None
        version = str(value.get("version") or "")
        if not version:
            return None
        return StrategySnapshot(policies=policies, disabled_ids=frozenset(), version=version, fallback_active=not bool(policies))

    async def inspect(self) -> dict[str, Any]:
        snapshot = await self.snapshot(force=True)
        documents, errors, _fingerprint = self._read_documents()
        return {
            "policy_dir": str(self._policy_dir),
            "version": snapshot.version,
            "fallback_active": snapshot.fallback_active,
            "policies": [
                {
                    "id": item.id,
                    "priority": item.priority,
                    "enabled": item.enabled,
                    "loaded": item in snapshot.policies,
                    "unloaded": item.id in snapshot.disabled_ids,
                    "applies_to": item.applies_to.model_dump(mode="json"),
                    "has_selection": item.selection is not None,
                    "has_interaction": item.interaction is not None,
                    "has_failure": item.failure is not None,
                }
                for item in documents
            ],
            "errors": errors,
        }

    async def reload(self) -> dict[str, Any]:
        await self.snapshot(force=True)
        return await self.inspect()

    async def unload(self, policy_id: str) -> dict[str, Any]:
        policy_id = str(policy_id or "").strip()
        if not policy_id or policy_id == _SAFE_FALLBACK.id:
            raise ValueError("不能卸载内置安全兜底策略")
        documents, _errors, _fingerprint = self._read_documents()
        if policy_id not in {item.id for item in documents}:
            raise KeyError(policy_id)
        self._local_disabled.add(policy_id)
        try:
            from app.core.redis import get_redis

            await get_redis().sadd(self._DISABLED_KEY, policy_id)
        except Exception:  # process-local state is the safe temporary fallback
            pass
        self._snapshot = None
        return await self.inspect()

    async def load(self, policy_id: str) -> dict[str, Any]:
        policy_id = str(policy_id or "").strip()
        documents, _errors, _fingerprint = self._read_documents()
        if policy_id not in {item.id for item in documents}:
            raise KeyError(policy_id)
        self._local_disabled.discard(policy_id)
        try:
            from app.core.redis import get_redis

            await get_redis().srem(self._DISABLED_KEY, policy_id)
        except Exception:
            pass
        self._snapshot = None
        return await self.inspect()

    @staticmethod
    def _applicable(snapshot: StrategySnapshot, profile: Any) -> tuple[StrategyPolicyDocument, ...]:
        policies = tuple(item for item in snapshot.policies if item.applies_to.matches(profile))
        return policies or (_SAFE_FALLBACK,)

    @staticmethod
    def _with_fallback(policies: tuple[StrategyPolicyDocument, ...]) -> tuple[StrategyPolicyDocument, ...]:
        return policies if _SAFE_FALLBACK in policies else (*policies, _SAFE_FALLBACK)

    @staticmethod
    def _selection_policy(snapshot: StrategySnapshot, profile: Any) -> tuple[str, SelectionPolicy]:
        for item in StrategyEngine._applicable(snapshot, profile):
            if item.selection is not None:
                return item.id, item.selection
        return _SAFE_FALLBACK.id, _SAFE_FALLBACK.selection  # pragma: no cover - invariant

    def select_implementation(
        self,
        candidates: list[StrategyCandidate],
        *,
        profile: Any,
        snapshot: StrategySnapshot,
        preference: SelectionMode | None = None,
    ) -> StrategySelection | None:
        if not candidates:
            return None
        policy_id, policy = self._selection_policy(snapshot, profile)
        mode = preference or policy.mode
        weights = policy.weights

        def score(candidate: StrategyCandidate) -> tuple[float, str]:
            reliability = float(candidate.reliability) if candidate.reliability is not None else 0.5
            value = candidate.capability_specificity * weights.capability_specificity
            value += weights.declared_capability_bonus if candidate.declared_capability else 0.0
            if mode == "economy":
                value -= max(0.0, candidate.cost) * max(1.0, weights.cost) * 10
                value += reliability * weights.reliability
            elif mode == "quality":
                value += reliability * max(1.0, weights.reliability) * 10
                value -= max(0.0, candidate.cost) * weights.cost * 0.1
            else:
                value += reliability * weights.reliability
                value -= max(0.0, candidate.cost) * weights.cost
            return value, candidate.name

        selected = sorted(candidates, key=lambda item: (-score(item)[0], score(item)[1]))[0]
        return StrategySelection(selected, policy_id, score(selected)[0], mode)

    def failure_action(self, *, category: str, profile: Any | None, snapshot: StrategySnapshot) -> FailureAction:
        policies = self._applicable(snapshot, profile) if profile is not None else (_SAFE_FALLBACK,)
        policies = self._with_fallback(policies)
        for item in policies:
            if item.failure and category in item.failure.actions:
                return item.failure.actions[category]
        return (_SAFE_FALLBACK.failure.actions.get(category) if _SAFE_FALLBACK.failure else None) or "replan"

    def should_clarify(
        self,
        *,
        profile: Any,
        has_prior_context: bool,
        snapshot: StrategySnapshot,
    ) -> tuple[bool, str]:
        for item in self._with_fallback(self._applicable(snapshot, profile)):
            policy = item.interaction
            if policy is None:
                continue
            low_confidence = float(getattr(profile, "confidence", 1.0)) < policy.confidence_below
            risky = _SAFETY_ORDER.get(str(getattr(profile, "safety_level", "READ_ONLY")), 0) >= _SAFETY_ORDER[policy.safety_at_or_above]
            context_missing = not has_prior_context
            if low_confidence and risky and (not policy.require_no_prior_context or context_missing):
                return True, item.id
        return False, ""


strategy_engine = StrategyEngine()
