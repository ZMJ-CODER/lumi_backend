"""从独立策略内核到应用路由类型的 Lumi 适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from lumi_orch.policy.engine import PolicyLoadError, RoutingPolicyEngine as KernelRoutingPolicyEngine

from app.agents.orchestration.policy.features import ROUTING_FEATURES, RoutingFeatureSnapshot
from app.agents.orchestration.policy.hooks import PolicyHookRegistry, policy_hooks
from app.agents.orchestration.policy.models import RoutingPolicyDocument
from app.agents.orchestration.task_routing import RouteChannel


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    channel: RouteChannel
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
    """Keep legacy call sites stable while delegating policy logic to lumi_orch."""

    def __init__(self, document: RoutingPolicyDocument, *, source: str, hooks: PolicyHookRegistry = policy_hooks) -> None:
        self._kernel = KernelRoutingPolicyEngine(
            document,
            source=source,
            feature_specs=ROUTING_FEATURES,
            hooks=hooks,
        )
        self.source = source

    @property
    def version(self) -> int:
        return self._kernel.version

    @property
    def policy_sha256(self) -> str:
        return self._kernel.policy_sha256

    @classmethod
    def from_path(cls, path: Path, *, hooks: PolicyHookRegistry = policy_hooks) -> "RoutingPolicyEngine":
        try:
            source = path.read_text(encoding="utf-8")
            document = RoutingPolicyDocument.model_validate(yaml.safe_load(source) or {})
        except Exception as exc:  # YAML/Pydantic/OSError are normalized at this adapter boundary.
            raise PolicyLoadError(f"routing policy load failed: {exc}") from exc
        return cls(document, source=source, hooks=hooks)

    def evaluate(self, features: RoutingFeatureSnapshot) -> PolicyDecision | None:
        decision = self._kernel.evaluate(features)
        if decision is None:
            return None
        return PolicyDecision(
            channel=RouteChannel(decision.channel),
            reason_code=decision.reason_code,
            rule_id=decision.rule_id,
            policy_version=decision.policy_version,
            policy_sha256=decision.policy_sha256,
            hooks=decision.hooks,
            requirements=decision.requirements,
            risk_level=decision.risk_level,
            require_clarification=decision.require_clarification,
            audit_metadata=decision.audit_metadata,
        )


__all__ = ["PolicyDecision", "PolicyLoadError", "RoutingPolicyEngine"]
