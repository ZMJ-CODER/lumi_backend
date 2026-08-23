from dataclasses import dataclass

from lumi_orch.effects import confirm_record, effect_intent_for_node, intent_record, uncertain_record


@dataclass
class Node:
    id: str = "send"
    agent: str = "mailer"
    params: dict = None


def test_intent_fingerprint_is_body_free_and_stable():
    node = Node(params={"to": "a@example.com", "preferred_tool": "send_email"})

    intent = effect_intent_for_node(job_id="job-1", node=node)

    assert intent["tool"] == "send_email"
    assert "to" not in intent
    assert len(intent["params_sha256"]) == 64


def test_effect_transition_preserves_intent_and_blocks_replay_after_interruption():
    intent = intent_record({"node_id": "send"}, now=10)
    uncertain = uncertain_record(intent, "task_cancelled", now=11)
    confirmed = confirm_record(intent, {"message_id": "m-1"}, now=12)

    assert uncertain.status == "uncertain"
    assert uncertain.intent == {"node_id": "send"}
    assert confirmed.status == "confirmed"
    assert confirmed.result == {"message_id": "m-1"}
