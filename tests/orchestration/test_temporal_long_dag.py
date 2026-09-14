from lumi_orch.job_spec import JobSpec, NodeSpec


def _payload(*, nodes, persisted_nodes=None):
    spec = JobSpec(
        job_id="long-job",
        user_id="user-1",
        nodes=tuple(nodes),
    ).with_fingerprint().model_dump(mode="json")
    return {
        "job_id": "long-job",
        "execution_spec": spec,
        "nodes": persisted_nodes or [],
        "config": {
            "long_dag": True,
            "use_node_child_workflows": True,
            "continue_as_new_after_nodes": 20,
        },
    }


def test_continue_as_new_restores_only_frozen_definition_and_terminal_state():
    from app.agents.temporal_workflows import _restore_frozen_nodes

    payload = _payload(
        nodes=[NodeSpec(id="read", agent="retrieval", params={"query": "原始"})],
        persisted_nodes=[{
            "id": "read",
            "agent": "evil-agent",
            "params": {"query": "被篡改"},
            "status": "completed",
            "result": None,
            "metadata": {
                "result_ref": {"id": "ref-1", "sha256": "a" * 64},
                "confirmed_tools": ["untrusted-side-effect"],
            },
        }],
    )

    restored = _restore_frozen_nodes(payload)

    assert restored[0]["agent"] == "retrieval"
    assert restored[0]["params"] == {"query": "原始"}
    assert restored[0]["status"] == "completed"
    assert restored[0]["metadata"]["result_ref"]["id"] == "ref-1"
    assert "confirmed_tools" not in restored[0]["metadata"]


def test_continue_as_new_compacts_completed_bodies_and_preserves_replan_state(monkeypatch):
    from app.agents.temporal_workflows import AgentDagWorkflow

    payload = _payload(nodes=[NodeSpec(id="read", agent="retrieval")])
    payload["nodes"] = [{
        "id": "read",
        "agent": "retrieval",
        "status": "completed",
        "result": {"content": "不应进入下一代 history"},
        "metadata": {"result_ref": {"id": "ref-1", "sha256": "b" * 64}},
    }]
    payload["routing"] = {"replan_count": 1, "temporal_continue_as_new_count": 2}
    instance = AgentDagWorkflow()
    instance._job = payload
    captured = {}

    def fake_continue_as_new(next_payload):
        captured.update(next_payload)
        raise RuntimeError("continued")

    monkeypatch.setattr("app.agents.temporal_workflows.workflow.continue_as_new", fake_continue_as_new)

    try:
        instance._continue_as_new(20)
    except RuntimeError as exc:
        assert str(exc) == "continued"

    assert captured["nodes"][0]["result"] is None
    assert captured["nodes"][0]["metadata"]["result_ref"]["id"] == "ref-1"
    assert captured["routing"]["replan_count"] == 1
    assert captured["routing"]["temporal_continue_as_new_count"] == 3
    assert "api_key" not in str(captured)


def test_long_dag_requires_references_before_history_cut():
    from app.agents.temporal_workflows import AgentDagWorkflow

    instance = AgentDagWorkflow()
    instance._job = {
        "nodes": [{"id": "read", "status": "completed", "result": {"content": "body"}, "metadata": {}}]
    }
    assert instance._can_continue_as_new() is False
    instance._job["nodes"][0]["metadata"]["result_ref"] = {"id": "ref", "sha256": "c" * 64}
    assert instance._can_continue_as_new() is True
    instance._job["nodes"] = [{"id": "empty", "status": "completed", "result": None, "metadata": {}}]
    assert instance._can_continue_as_new() is True
