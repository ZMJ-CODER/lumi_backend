import asyncio

from lumi_orch.job_spec import JobSpec, NodeSpec


def test_temporal_replan_rejects_effectful_or_approved_specs_before_loading_context():
    from app.agents.orchestration.temporal.activities import replan_static_job_activity

    effectful = JobSpec(
        job_id="replan-effect",
        user_id="u1",
        nodes=(NodeSpec(
            id="edit",
            agent="office_doc",
            params={"mode": "edit", "doc_id": "d1"},
            approval=True,
            idempotency_key="effect-key",
        ),),
    ).with_fingerprint()

    result = asyncio.run(replan_static_job_activity({
        "job_id": "replan-effect",
        "execution_spec": effectful.model_dump(mode="json"),
        "nodes": [],
    }))

    assert result == {"allowed": False, "reason": "effectful_or_approval_job"}


def test_temporal_workflow_rejects_tampered_replacement_spec():
    from app.agents.temporal_workflows import _spec_fingerprint_matches

    spec = JobSpec(
        job_id="replan-read",
        user_id="u1",
        nodes=(NodeSpec(id="read", agent="retrieval", params={"query": "预算"}),),
    ).with_fingerprint().model_dump(mode="json")

    assert _spec_fingerprint_matches(spec) is True
    spec["nodes"][0]["params"]["query"] = "被篡改的查询"
    assert _spec_fingerprint_matches(spec) is False
