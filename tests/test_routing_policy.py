"""Contract tests for the constrained declarative routing-policy boundary."""

from pathlib import Path

import pytest

from app.agents.orchestration.policy.engine import PolicyDecision, PolicyLoadError, RoutingPolicyEngine
from app.agents.orchestration.policy.features import build_routing_features
from app.agents.orchestration.policy.models import RoutingPolicyDocument
from app.agents.orchestration.task_routing import (
    RouteChannel,
    _route_atomic_instruction_legacy,
    route_atomic_instruction,
)


def test_deployment_policy_matches_the_existing_single_file_conversion_route():
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "routing_rules.yaml"
    engine = RoutingPolicyEngine.from_path(path)
    features = build_routing_features(
        "请转换文件为 csv 格式",
        has_authorized_documents=True,
        office_document_count=1,
    )

    decision = engine.evaluate(features)

    assert decision is not None
    assert decision.rule_id == "explicit_single_file_conversion"
    assert decision.channel == RouteChannel.DETERMINISTIC_SCRIPT
    assert route_atomic_instruction(
        "请转换文件为 csv 格式",
        has_authorized_documents=True,
        office_document_count=1,
    ).channel == decision.channel


def test_deployment_policy_marks_multi_document_factual_lookup_for_discovery():
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "routing_rules.yaml"
    engine = RoutingPolicyEngine.from_path(path)
    features = build_routing_features(
        "这七份资料里，哪一份写了付款期限和违约条款？",
        has_authorized_documents=True,
        office_document_count=7,
    )

    decision = engine.evaluate(features)

    assert decision is not None
    assert decision.rule_id == "multi_document_factual_targeting"
    assert decision.channel == RouteChannel.RAG
    assert decision.requirements == ("document_discovery", "scoped_document_read")
    assert dict(decision.audit_metadata)["document_discovery_required"] == "true"


def test_multi_document_which_one_colloquial_shape_is_a_factual_targeting_feature():
    features = build_routing_features(
        "这七份资料里，哪一份写了付款期限？",
        has_authorized_documents=True,
        office_document_count=7,
    )

    assert features.is_factual_document_question is True


def test_multi_document_english_factual_shape_is_a_targeting_feature():
    features = build_routing_features(
        "Which document contains the payment terms and termination clause?",
        has_authorized_documents=True,
        office_document_count=4,
    )

    assert features.is_factual_document_question is True
    assert features.requires_multi_document_targeting is True


@pytest.mark.parametrize(("instruction", "documents", "expected_rule"), [
    ("请从知识库查询付款条件", False, "explicit_rag_lookup"),
    ("帮我总结这个附件", True, "authorized_document_lookup"),
])
def test_deployment_policy_matches_stable_retrieval_routes(instruction, documents, expected_rule):
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "routing_rules.yaml"
    engine = RoutingPolicyEngine.from_path(path)
    features = build_routing_features(
        instruction,
        has_authorized_documents=documents,
        office_document_count=1 if documents else 0,
    )

    decision = engine.evaluate(features)

    assert decision is not None
    assert decision.rule_id == expected_rule
    assert decision.channel == RouteChannel.RAG
    assert route_atomic_instruction(
        instruction,
        has_authorized_documents=documents,
        office_document_count=1 if documents else 0,
    ).channel == RouteChannel.RAG


@pytest.mark.parametrize("instruction", [
    "先找出付款条款，再把结论发邮件给法务。",
    "查一下哪份文件写了付款期限，然后更新采购系统。",
    "核对这七份附件是否符合付款条款。",
])
def test_compound_or_stateful_requests_use_generic_agent_coordination_not_targeting_hook(instruction: str):
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "routing_rules.yaml"
    engine = RoutingPolicyEngine.from_path(path)
    features = build_routing_features(
        instruction,
        has_authorized_documents=True,
        office_document_count=7,
    )

    decision = engine.evaluate(features)

    assert decision is not None
    assert decision.rule_id == "agent_coordination"
    assert decision.channel == RouteChannel.AGENT
    assert decision.hooks == ()


def test_external_system_update_is_a_safety_feature_even_with_colloquial_wording():
    features = build_routing_features(
        "查一下哪份文件写了付款期限，然后更新采购系统。",
        has_authorized_documents=True,
        office_document_count=7,
    )

    assert features.has_external_operation is True


def test_policy_rejects_unknown_features_at_load_time():
    document = RoutingPolicyDocument.model_validate({
        "version": 1,
        "rules": [{
            "id": "unknown_feature_rule",
            "priority": 10,
            "when": [{"feature": "user_is_vip", "op": "eq", "value": True}],
            "channel": "direct_llm",
            "reason_code": "unknown_feature",
        }],
    })

    with pytest.raises(PolicyLoadError, match="unknown routing feature"):
        RoutingPolicyEngine(document, source="test")


def test_policy_rejects_unregistered_hook_at_load_time():
    document = RoutingPolicyDocument.model_validate({
        "version": 1,
        "rules": [{
            "id": "unsafe_dynamic_hook",
            "priority": 10,
            "when": [{"feature": "has_authorized_documents", "op": "eq", "value": True}],
            "channel": "rag",
            "reason_code": "document_lookup",
            "hooks": ["someone.module.run"],
        }],
    })

    with pytest.raises(PolicyLoadError, match="unregistered policy hook"):
        RoutingPolicyEngine(document, source="test")


def test_policy_rejects_overlapping_decisions_without_explicit_precedence():
    document = RoutingPolicyDocument.model_validate({
        "version": 1,
        "rules": [
            {
                "id": "any_document",
                "priority": 20,
                "when": [{"feature": "has_authorized_documents", "op": "eq", "value": True}],
                "channel": "rag",
                "reason_code": "document_lookup",
            },
            {
                "id": "many_documents",
                "priority": 10,
                "when": [{"feature": "office_document_count", "op": "gte", "value": 2}],
                "channel": "agent",
                "reason_code": "document_targeting",
            },
        ],
    })

    with pytest.raises(PolicyLoadError, match="may overlap"):
        RoutingPolicyEngine(document, source="test")


def test_shadow_mode_evaluates_a_candidate_but_keeps_the_legacy_route(monkeypatch):
    class DivergentPolicy:
        policy_sha256 = "policy-test"

        @staticmethod
        def evaluate(_features):
            return PolicyDecision(
                channel=RouteChannel.AGENT,
                reason_code="multi_document_targeting",
                rule_id="test_divergence",
                policy_version=1,
                policy_sha256="policy-test",
                hooks=(),
            )

    monkeypatch.setattr(
        "app.agents.orchestration.task_routing._load_routing_policy",
        lambda: DivergentPolicy(),
    )
    monkeypatch.setattr(
        "app.agents.orchestration.task_routing._routing_policy_mode",
        lambda: "shadow",
    )

    decision = route_atomic_instruction("请帮我写一段会议纪要")

    assert decision.channel == RouteChannel.DIRECT_LLM


def test_policy_enforce_mode_is_explicit_opt_in(monkeypatch):
    class DivergentPolicy:
        policy_sha256 = "policy-test"

        @staticmethod
        def evaluate(_features):
            return PolicyDecision(
                channel=RouteChannel.AGENT,
                reason_code="multi_document_targeting",
                rule_id="test_divergence",
                policy_version=1,
                policy_sha256="policy-test",
                hooks=(),
            )

    monkeypatch.setattr(
        "app.agents.orchestration.task_routing._load_routing_policy",
        lambda: DivergentPolicy(),
    )
    monkeypatch.setattr(
        "app.agents.orchestration.task_routing._routing_policy_mode",
        lambda: "enforce",
    )

    decision = route_atomic_instruction("请帮我写一段会议纪要")

    assert decision.channel == RouteChannel.AGENT


@pytest.mark.parametrize(("instruction", "documents", "count", "expected"), [
    ("请从知识库查询付款条件", False, 0, RouteChannel.RAG),
    ("帮我总结这个附件", True, 1, RouteChannel.RAG),
    ("这七份资料里，哪一份写了付款期限？", True, 7, RouteChannel.RAG),
    ("先读取附件，再把结论发送给法务", True, 1, RouteChannel.AGENT),
    ("帮我核对这份资料是否符合付款条款", True, 1, RouteChannel.AGENT),
    ("请写一段会议纪要", False, 0, RouteChannel.DIRECT_LLM),
])
def test_migrated_rules_remain_equivalent_when_enforced(monkeypatch, instruction, documents, count, expected):
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "routing_rules.yaml"
    policy = RoutingPolicyEngine.from_path(path)
    monkeypatch.setattr(
        "app.agents.orchestration.task_routing._load_routing_policy",
        lambda: policy,
    )
    monkeypatch.setattr(
        "app.agents.orchestration.task_routing._routing_policy_mode",
        lambda: "enforce",
    )

    decision = route_atomic_instruction(
        instruction,
        has_authorized_documents=documents,
        office_document_count=count,
    )

    assert decision.channel == expected


def test_derived_features_keep_routing_precedence_out_of_yaml_conditions():
    coordination = build_routing_features(
        "先读取附件，再把结论发送给法务",
        has_authorized_documents=True,
        office_document_count=1,
    )
    direct = build_routing_features(
        "请写一段会议纪要",
        has_authorized_documents=False,
        office_document_count=0,
    )

    assert coordination.requires_agent_coordination is True
    assert coordination.can_direct_respond is False
    assert direct.can_direct_respond is True


@pytest.mark.parametrize(("instruction", "documents", "count"), [
    ("请转换文件为 csv 格式", True, 1),
    ("这七份资料里，哪一份写了付款期限？", True, 7),
    ("请从知识库查询付款条件", False, 0),
    ("帮我总结这个附件", True, 1),
    ("先读取附件，再把结论发送给法务", True, 1),
    ("帮我核对这份资料是否符合付款条款", True, 1),
    ("请写一段会议纪要", False, 0),
])
def test_policy_rule_table_covers_every_legacy_atomic_route(instruction, documents, count):
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "routing_rules.yaml"
    policy = RoutingPolicyEngine.from_path(path)
    features = build_routing_features(
        instruction,
        has_authorized_documents=documents,
        office_document_count=count,
    )

    candidate = policy.evaluate(features)
    legacy = _route_atomic_instruction_legacy(
        instruction,
        has_authorized_documents=documents,
        office_document_count=count,
    )

    assert candidate is not None
    assert candidate.channel == legacy.channel
