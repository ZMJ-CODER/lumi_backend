from lumi_orch.job_spec import JobSpec, NodeSpec


def test_job_spec_fingerprint_is_stable_and_binds_node_parameters():
    base = JobSpec(
        job_id="job-1",
        user_id="user-1",
        request="读取文件",
        nodes=(NodeSpec(id="read", agent="office_doc", params={"doc_id": "d1", "mode": "read"}),),
    )
    first = base.with_fingerprint()
    again = base.with_fingerprint()
    changed = base.model_copy(update={
        "nodes": (NodeSpec(id="read", agent="office_doc", params={"doc_id": "d2", "mode": "read"}),),
    }).with_fingerprint()

    assert first.fingerprint == again.fingerprint
    assert first.fingerprint != changed.fingerprint
