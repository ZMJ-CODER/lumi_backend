"""受控策略钩子接口；具体钩子实现由应用层持有。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class PolicyAdjustment:
    requirements: frozenset[str] = frozenset()
    raise_risk_to: str | None = None
    require_clarification: bool = False
    audit_metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationVerdict:
    status: str
    code: str
    message: str = ""


class PreRouteHook(Protocol):
    name: str

    def apply(self, *, decision: Any, features: Any) -> PolicyAdjustment: ...


class NodePolicyHook(Protocol):
    name: str

    def apply(self, *, node: Any, upstream_results: dict[str, Any]) -> dict[str, Any]: ...


class ResultVerifierHook(Protocol):
    name: str

    def check(self, *, result: Any, context: Any) -> VerificationVerdict: ...


class PolicyHookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, object] = {}

    def register(self, hook: object) -> None:
        name = str(getattr(hook, "name", "") or "")
        if not name:
            raise ValueError("policy hooks require a stable name")
        if name in self._hooks:
            raise ValueError(f"duplicate policy hook: {name}")
        self._hooks[name] = hook

    def has(self, name: str) -> bool:
        return name in self._hooks

    def get_pre_route(self, name: str) -> PreRouteHook:
        hook = self._hooks.get(name)
        if hook is None or not hasattr(hook, "apply"):
            raise KeyError(f"unregistered pre-route hook: {name}")
        return hook  # type: ignore[return-value]


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def merge_adjustments(adjustments: list[PolicyAdjustment]) -> PolicyAdjustment:
    requirements: set[str] = set()
    audit_metadata: dict[str, str] = {}
    risk: str | None = None
    clarification = False
    for adjustment in adjustments:
        requirements.update(adjustment.requirements)
        clarification = clarification or adjustment.require_clarification
        if adjustment.raise_risk_to:
            if adjustment.raise_risk_to not in _RISK_ORDER:
                raise ValueError(f"unsupported policy risk level: {adjustment.raise_risk_to}")
            if risk is None or _RISK_ORDER[adjustment.raise_risk_to] > _RISK_ORDER[risk]:
                risk = adjustment.raise_risk_to
        for key, value in adjustment.audit_metadata:
            if key in audit_metadata and audit_metadata[key] != value:
                raise ValueError(f"conflicting policy audit metadata: {key}")
            audit_metadata[key] = value
    return PolicyAdjustment(
        requirements=frozenset(requirements),
        raise_risk_to=risk,
        require_clarification=clarification,
        audit_metadata=tuple(sorted(audit_metadata.items())),
    )
