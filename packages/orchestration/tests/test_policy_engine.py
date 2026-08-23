from dataclasses import dataclass

import pytest

from lumi_orch.policy.engine import PolicyLoadError, RoutingPolicyEngine
from lumi_orch.policy.lexicon_models import RoutingLexiconDocument
from lumi_orch.policy.planning_models import PlanningPolicyDocument
from lumi_orch.policy.models import RoutingPolicyDocument
from lumi_orch.policy.tca_models import TcaPolicyDocument
from lumi_orch.policy.registry import PolicyHookRegistry


@dataclass(frozen=True)
class FeatureSpec:
    value_type: str


@dataclass(frozen=True)
class Features:
    schema_version: int = 1
    has_documents: bool = True

    def value_for(self, feature: str):
        return getattr(self, feature)


def test_engine_is_independent_of_application_route_enum():
    document = RoutingPolicyDocument.model_validate({
        "version": 1,
        "rules": [{
            "id": "documents",
            "priority": 10,
            "when": [{"feature": "has_documents", "op": "eq", "value": True}],
            "channel": "rag",
            "reason_code": "document_lookup",
        }],
    })
    engine = RoutingPolicyEngine(
        document,
        source="test",
        feature_specs={"has_documents": FeatureSpec("bool")},
        hooks=PolicyHookRegistry(),
    )

    decision = engine.evaluate(Features())

    assert decision is not None
    assert decision.channel == "rag"


def test_engine_rejects_an_unknown_feature_without_application_context():
    document = RoutingPolicyDocument.model_validate({
        "version": 1,
        "rules": [{
            "id": "unknown",
            "priority": 10,
            "when": [{"feature": "user_is_vip", "op": "eq", "value": True}],
            "channel": "agent",
            "reason_code": "unknown",
        }],
    })

    with pytest.raises(PolicyLoadError, match="unknown routing feature"):
        RoutingPolicyEngine(
            document,
            source="test",
            feature_specs={"has_documents": FeatureSpec("bool")},
            hooks=PolicyHookRegistry(),
        )


def test_tca_policy_rejects_unbalanced_weights():
    with pytest.raises(Exception, match="sum to 1.0"):
        TcaPolicyDocument.model_validate({
            "version": 1,
            "weights": {
                "entity_count": 0.3, "implicitness": 0.3, "dependency": 0.3,
                "ambiguity": 0.3, "history_dependency": 0.3,
            },
            "thresholds": {
                "explicit_workflow_dependency": 0.35, "m2_dependency": 0.35,
                "m3_ambiguity": 0.65, "classifier_confidence": 0.7, "history_dynamic": 0.6,
            },
        })


def test_routing_lexicon_contract_rejects_unknown_actions_and_duplicate_markers():
    with pytest.raises(Exception):
        RoutingLexiconDocument.model_validate({
            "version": 1,
            "actions": [{"id": "invented", "risk_level": "read_only", "markers": ["do"]}],
            "objects": [{"id": "document", "markers": ["file"]}],
        })


def test_planning_policy_rejects_unknown_templates_and_duplicate_markers():
    with pytest.raises(Exception):
        PlanningPolicyDocument.model_validate({
            "version": 1,
            "template_markers": [{"name": "invented_flow", "markers": ["x"]}],
            "semi_structure_markers": ["if"],
            "script_markers": ["export"],
            "multi_topic_markers": ["then"],
        })
    with pytest.raises(Exception, match="unique"):
        PlanningPolicyDocument.model_validate({
            "version": 1,
            "template_markers": [{"name": "daily_brief_flow", "markers": ["早报", "早报"]}],
            "semi_structure_markers": ["if"],
            "script_markers": ["export"],
            "multi_topic_markers": ["then"],
        })
    with pytest.raises(Exception, match="unique"):
        RoutingLexiconDocument.model_validate({
            "version": 1,
            "actions": [{"id": "query", "risk_level": "read_only", "markers": ["查", "查"]}],
            "objects": [{"id": "document", "markers": ["文件"]}],
        })
