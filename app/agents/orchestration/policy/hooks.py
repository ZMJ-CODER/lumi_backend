"""由应用层实现的策略钩子。"""

from __future__ import annotations

from typing import Any

from lumi_orch.policy.registry import (
    NodePolicyHook,
    PolicyAdjustment,
    PolicyHookRegistry,
    PreRouteHook,
    ResultVerifierHook,
    VerificationVerdict,
    merge_adjustments,
)


class DocumentTargetingHook:
    """Require discovery before a multi-document factual answer is produced."""

    name = "document_targeting"

    def apply(self, *, decision: Any, features: Any) -> PolicyAdjustment:
        # The policy conditions already narrow this route.  Keep the check in
        # the hook as defence in depth because hooks remain callable objects.
        if features.office_document_count < 2 or not features.is_factual_document_question:
            return PolicyAdjustment(require_clarification=True)
        return PolicyAdjustment(
            requirements=frozenset({"document_discovery", "scoped_document_read"}),
            audit_metadata=(
                ("document_discovery_required", "true"),
                ("document_targeting_strategy", "inspect_then_scoped_read"),
            ),
        )


policy_hooks = PolicyHookRegistry()
policy_hooks.register(DocumentTargetingHook())

__all__ = [
    "NodePolicyHook", "PolicyAdjustment", "PolicyHookRegistry", "PreRouteHook",
    "ResultVerifierHook", "VerificationVerdict", "merge_adjustments", "policy_hooks",
]
