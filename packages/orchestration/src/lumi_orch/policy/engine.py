"""不依赖应用层导入、经过校验的 YAML 策略匹配器。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml
from pydantic import ValidationError

from lumi_orch.policy.models import PolicyCondition, RoutingPolicyDocument, RoutingPolicyRule
from lumi_orch.policy.registry import PolicyHookRegistry, merge_adjustments


class FeatureSpec(Protocol):
    value_type: str


class RoutingFeatures(Protocol):
    schema_version: int

    def value_for(self, feature: str) -> Any: ...


class PolicyLoadError(ValueError):
    """Raised before malformed policy can become active."""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    channel: str
    reason_code: str
    rule_id: str
    policy_version: int
    policy_sha256: str
    hooks: tuple[str, ...]
    requirements: tuple[str, ...] = ()
    risk_level: str | None = None
    require_clarification: bool = False
    audit_metadata: tuple[tuple[str, str], ...] = ()


class RoutingPolicyEngine:
    """Evaluate conjunction-only rules against a closed feature registry."""

    def __init__(
        self,
        document: RoutingPolicyDocument,
        *,
        source: str,
        feature_specs: Mapping[str, FeatureSpec],
        hooks: PolicyHookRegistry,
    ) -> None:
        self._document = document
        self.source = source
        self._feature_specs = feature_specs
        self._hooks = hooks
        self.policy_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        self._rules = tuple(sorted(document.rules, key=lambda rule: rule.priority, reverse=True))
        self._lint()

    @property
    def version(self) -> int:
        return self._document.version

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        feature_specs: Mapping[str, FeatureSpec],
        hooks: PolicyHookRegistry,
    ) -> "RoutingPolicyEngine":
        try:
            source = path.read_text(encoding="utf-8")
            document = RoutingPolicyDocument.model_validate(yaml.safe_load(source) or {})
        except (OSError, ValidationError, yaml.YAMLError) as exc:
            raise PolicyLoadError(f"routing policy load failed: {exc}") from exc
        return cls(document, source=source, feature_specs=feature_specs, hooks=hooks)

    def evaluate(self, features: RoutingFeatures) -> PolicyDecision | None:
        if features.schema_version != self.version:
            raise PolicyLoadError(
                f"routing policy version {self.version} cannot consume feature schema {features.schema_version}"
            )
        for rule in self._rules:
            if all(self._matches(condition, features.value_for(condition.feature)) for condition in rule.when):
                try:
                    adjustment = merge_adjustments([
                        self._hooks.get_pre_route(hook).apply(decision=rule, features=features)
                        for hook in rule.hooks
                    ])
                except (KeyError, ValueError) as exc:
                    raise PolicyLoadError(f"{rule.id}: policy hook evaluation failed: {exc}") from exc
                return PolicyDecision(
                    channel=rule.channel,
                    reason_code=rule.reason_code,
                    rule_id=rule.id,
                    policy_version=self.version,
                    policy_sha256=self.policy_sha256,
                    hooks=rule.hooks,
                    requirements=tuple(sorted(adjustment.requirements)),
                    risk_level=adjustment.raise_risk_to,
                    require_clarification=adjustment.require_clarification,
                    audit_metadata=adjustment.audit_metadata,
                )
        return None

    def _lint(self) -> None:
        for rule in self._rules:
            for condition in rule.when:
                spec = self._feature_specs.get(condition.feature)
                if spec is None:
                    raise PolicyLoadError(f"{rule.id}: unknown routing feature {condition.feature}")
                self._validate_condition(rule, condition, spec)
            for hook in rule.hooks:
                if not self._hooks.has(hook):
                    raise PolicyLoadError(f"{rule.id}: unregistered policy hook {hook}")
        for index, left in enumerate(self._rules):
            for right in self._rules[index + 1:]:
                if left.channel == right.channel and left.hooks == right.hooks:
                    continue
                if self._may_overlap(left, right) and not self._declares_precedence(left, right):
                    raise PolicyLoadError(
                        f"routing rules {left.id} and {right.id} may overlap with different decisions; "
                        "declare overrides explicitly"
                    )

    @staticmethod
    def _declares_precedence(left: RoutingPolicyRule, right: RoutingPolicyRule) -> bool:
        return right.id in left.overrides or left.id in right.overrides

    @staticmethod
    def _validate_condition(rule: RoutingPolicyRule, condition: PolicyCondition, spec: FeatureSpec) -> None:
        allowed = {
            "bool": {"eq"},
            "int": {"eq", "gte", "lte"},
            "str_set": {"contains"},
        }.get(spec.value_type)
        if allowed is None or condition.op not in allowed:
            raise PolicyLoadError(f"{rule.id}: {condition.op} is invalid for {condition.feature}")
        if spec.value_type == "bool" and not isinstance(condition.value, bool):
            raise PolicyLoadError(f"{rule.id}: {condition.feature} requires a boolean value")
        if spec.value_type == "int" and (not isinstance(condition.value, int) or isinstance(condition.value, bool)):
            raise PolicyLoadError(f"{rule.id}: {condition.feature} requires an integer value")
        if spec.value_type == "str_set" and not isinstance(condition.value, str):
            raise PolicyLoadError(f"{rule.id}: {condition.feature} requires a string value")

    @staticmethod
    def _matches(condition: PolicyCondition, actual: Any) -> bool:
        if condition.op == "eq":
            return actual == condition.value
        if condition.op == "gte":
            return actual >= condition.value
        if condition.op == "lte":
            return actual <= condition.value
        if condition.op == "contains":
            return condition.value in actual
        raise PolicyLoadError(f"unsupported policy operator: {condition.op}")

    def _may_overlap(self, left: RoutingPolicyRule, right: RoutingPolicyRule) -> bool:
        left_by_feature = {condition.feature: condition for condition in left.when}
        right_by_feature = {condition.feature: condition for condition in right.when}
        for feature in set(left_by_feature) & set(right_by_feature):
            if not self._conditions_overlap(left_by_feature[feature], right_by_feature[feature]):
                return False
        return True

    @staticmethod
    def _conditions_overlap(left: PolicyCondition, right: PolicyCondition) -> bool:
        if left.op == right.op == "eq":
            return left.value == right.value
        numeric = {"eq", "gte", "lte"}
        if left.op in numeric and right.op in numeric:
            lower_values = [
                value for value in (
                    left.value if left.op in {"eq", "gte"} else None,
                    right.value if right.op in {"eq", "gte"} else None,
                ) if value is not None
            ]
            upper_values = [
                value for value in (
                    left.value if left.op in {"eq", "lte"} else None,
                    right.value if right.op in {"eq", "lte"} else None,
                ) if value is not None
            ]
            return not lower_values or not upper_values or max(lower_values) <= min(upper_values)
        return True
