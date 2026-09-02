from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.orchestration.policy import lexicon
from app.agents.orchestration.routing_intent import infer_route_intent
from lumi_orch.policy.lexicon_models import RoutingLexiconDocument


def test_deployment_lexicon_loads_and_preserves_action_shape():
    lexicon.load_routing_lexicon.cache_clear()
    document = lexicon.load_routing_lexicon()

    assert document.version == 1
    assert {entry.id for entry in document.actions} == {
        "lookup_history", "converse", "send", "execute", "modify", "transform",
        "create", "analyze", "read", "query",
    }
    assert any("查一下" in entry.markers for entry in document.actions if entry.id == "query")


def test_intent_router_consumes_the_deployment_lexicon():
    intent = infer_route_intent("帮我查一下这个文件里有没有付款期限")

    assert "query" in intent.actions
    assert "document" in intent.objects


def test_lexicon_path_is_a_checked_in_deployment_asset():
    path = Path(__file__).resolve().parents[1] / "config" / "agent_policies" / "routing_lexicon.yaml"

    assert path.is_file()


def test_lexicon_rejects_unknown_policy_fields():
    with pytest.raises(ValidationError):
        RoutingLexiconDocument.model_validate({
            "version": 1,
            "actions": [{"id": "query", "risk_level": "read_only", "markers": ["查"]}],
            "objects": [{"id": "data", "markers": ["数据"]}],
            "unexpected": True,
        })
