"""封闭路由特征集合及其唯一计算入口。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from app.agents.orchestration.routing_patterns import (
    EXTERNAL_OPERATION,
    FACTUAL_DOCUMENT_QUESTION,
    file_operation_matches,
    MULTI_OPERATION,
    rag_operation_matches,
    STATEFUL_REASONING,
    agent_operation_matches,
)


FeatureType = Literal["bool", "int", "str_set"]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One governed feature available to policy documents."""

    key: str
    value_type: FeatureType
    owner: str
    policy_domains: frozenset[str]


ROUTING_FEATURES: dict[str, FeatureSpec] = {
    "has_authorized_documents": FeatureSpec("has_authorized_documents", "bool", "routing.features", frozenset({"routing"})),
    "office_document_count": FeatureSpec("office_document_count", "int", "routing.features", frozenset({"routing"})),
    "has_explicit_file_operation": FeatureSpec("has_explicit_file_operation", "bool", "routing.features", frozenset({"routing"})),
    "has_rag_operation": FeatureSpec("has_rag_operation", "bool", "routing.features", frozenset({"routing"})),
    "has_authorized_document_lookup_intent": FeatureSpec("has_authorized_document_lookup_intent", "bool", "routing.features", frozenset({"routing"})),
    "has_external_operation": FeatureSpec("has_external_operation", "bool", "routing.features", frozenset({"routing"})),
    "has_multi_operation": FeatureSpec("has_multi_operation", "bool", "routing.features", frozenset({"routing"})),
    "has_stateful_reasoning": FeatureSpec("has_stateful_reasoning", "bool", "routing.features", frozenset({"routing"})),
    "is_factual_document_question": FeatureSpec("is_factual_document_question", "bool", "routing.features", frozenset({"routing"})),
    # These derived features are the governed, single-point encoding of legacy
    # precedence. YAML consumes them instead of reconstructing boolean logic.
    "requires_agent_coordination": FeatureSpec("requires_agent_coordination", "bool", "routing.features", frozenset({"routing"})),
    "requires_multi_document_targeting": FeatureSpec("requires_multi_document_targeting", "bool", "routing.features", frozenset({"routing"})),
    "requires_retrieval": FeatureSpec("requires_retrieval", "bool", "routing.features", frozenset({"routing"})),
    "can_direct_respond": FeatureSpec("can_direct_respond", "bool", "routing.features", frozenset({"routing"})),
    "document_kinds": FeatureSpec("document_kinds", "str_set", "routing.features", frozenset({"routing"})),
}


@dataclass(frozen=True, slots=True)
class RoutingFeatureSnapshot:
    """Immutable, versioned facts consumed by routing policy only.

    Presentation state, user identity and raw request text do not enter this
    contract.  Safety and authorization remain enforced outside the policy
    engine.
    """

    schema_version: Literal[1]
    has_authorized_documents: bool
    office_document_count: int
    has_explicit_file_operation: bool
    has_rag_operation: bool
    has_authorized_document_lookup_intent: bool
    has_external_operation: bool
    has_multi_operation: bool
    has_stateful_reasoning: bool
    is_factual_document_question: bool
    requires_agent_coordination: bool
    requires_multi_document_targeting: bool
    requires_retrieval: bool
    can_direct_respond: bool
    document_kinds: frozenset[str]

    def value_for(self, feature: str) -> Any:
        if feature not in ROUTING_FEATURES:
            raise KeyError(f"unregistered routing feature: {feature}")
        return getattr(self, feature)

    def audit_dict(self) -> dict[str, Any]:
        """Safe, bounded fields for decision logs; never includes raw text."""
        return {
            "schema_version": self.schema_version,
            "has_authorized_documents": self.has_authorized_documents,
            "office_document_count": self.office_document_count,
            "has_explicit_file_operation": self.has_explicit_file_operation,
            "has_rag_operation": self.has_rag_operation,
            "has_authorized_document_lookup_intent": self.has_authorized_document_lookup_intent,
            "has_external_operation": self.has_external_operation,
            "has_multi_operation": self.has_multi_operation,
            "has_stateful_reasoning": self.has_stateful_reasoning,
            "is_factual_document_question": self.is_factual_document_question,
            "requires_agent_coordination": self.requires_agent_coordination,
            "requires_multi_document_targeting": self.requires_multi_document_targeting,
            "requires_retrieval": self.requires_retrieval,
            "can_direct_respond": self.can_direct_respond,
            "document_kinds": sorted(self.document_kinds),
        }


def build_routing_features(
    instruction: str,
    *,
    has_authorized_documents: bool,
    office_document_count: int,
    office_documents: list[dict[str, Any]] | None = None,
) -> RoutingFeatureSnapshot:
    """Compute every v1 routing feature once, before policy evaluation."""
    text = (instruction or "").strip()
    documents = office_documents or []
    kinds = frozenset(
        str(item.get("kind") or "").strip().lower()
        for item in documents
        if str(item.get("kind") or "").strip()
    )
    has_file_operation = file_operation_matches(text)
    has_rag_operation = rag_operation_matches(text)
    has_authorized_document_lookup_intent = bool(
        has_authorized_documents
        and re.search(r"(?iu)(?:查|找|问答|总结|提取|分析|检索|回答|说明)", text)
    )
    has_external_operation = bool(EXTERNAL_OPERATION.search(text))
    has_multi_operation = bool(MULTI_OPERATION.search(text))
    has_stateful_reasoning = bool(STATEFUL_REASONING.search(text))
    has_agent_operation = agent_operation_matches(text)
    # Plain drafting such as “改成正式通知语气” is a direct response, not a
    # notification delivery operation.  Delivery/update verbs are covered by
    # EXTERNAL_OPERATION when their state target (mail/system/etc.) is present.
    has_feedback_repair = bool(re.search(
        r"(?iu)(?:结果.{0,24}(?:不对|错误)|能.{0,12}修|自动修|修复|改正|纠正|重新处理|"
        r"(?:如果|若).{0,80}(?:否则|不然))",
        text,
    ))
    is_factual_document_question = bool(FACTUAL_DOCUMENT_QUESTION.search(text))
    requires_agent_coordination = any((
        has_multi_operation,
        has_external_operation,
        has_stateful_reasoning,
        has_agent_operation,
        has_feedback_repair,
    ))
    # Targeting is a read-only fixed path. A request that also asks to repair,
    # send, update or otherwise coordinate work must retain the richer agent
    # route even if it happens to mention multiple attachments.
    requires_multi_document_targeting = (
        office_document_count >= 2
        and is_factual_document_question
        and not requires_agent_coordination
        and not has_feedback_repair
    )
    requires_retrieval = has_rag_operation or has_authorized_document_lookup_intent
    can_direct_respond = not any((
        requires_agent_coordination,
        requires_multi_document_targeting,
        has_file_operation,
        requires_retrieval,
    ))
    return RoutingFeatureSnapshot(
        schema_version=1,
        has_authorized_documents=bool(has_authorized_documents),
        office_document_count=max(0, int(office_document_count)),
        has_explicit_file_operation=has_file_operation,
        has_rag_operation=has_rag_operation,
        has_authorized_document_lookup_intent=has_authorized_document_lookup_intent,
        has_external_operation=has_external_operation,
        has_multi_operation=has_multi_operation,
        has_stateful_reasoning=has_stateful_reasoning,
        is_factual_document_question=is_factual_document_question,
        requires_agent_coordination=requires_agent_coordination,
        requires_multi_document_targeting=requires_multi_document_targeting,
        requires_retrieval=requires_retrieval,
        can_direct_respond=can_direct_respond,
        document_kinds=kinds,
    )
