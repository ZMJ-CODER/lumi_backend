import json
import asyncio

from app.agents.orchestration.models import Job, JobStatus, TaskNode
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.state import InMemoryStateStore, _decode_job


def test_legacy_redis_empty_arrays_are_repaired_before_job_validation():
    job = Job(job_id="j1", user_id="u1", request="test", nodes=[TaskNode(id="n1", agent="atomic_step")])
    payload = job.model_dump()
    payload["nodes"][0]["depends_on"] = {}
    payload["nodes"][0]["resource_claims"] = {}

    decoded = _decode_job(json.dumps(payload), "j1")

    assert decoded is not None
    assert decoded.nodes[0].depends_on == []
    assert decoded.nodes[0].resource_claims == []


def test_legacy_running_job_uses_local_snapshot_when_state_store_temporarily_misses():
    async def scenario():
        orchestrator = AgentOrchestrator(store=InMemoryStateStore(), temporal_enabled=False)
        job = Job(job_id="live-job", user_id="u1", request="转换文件", status=JobStatus.RUNNING)
        orchestrator._live_jobs[job.job_id] = job
        return await orchestrator.get_job(job.job_id)

    restored = asyncio.run(scenario())
    assert restored is not None
    assert restored.job_id == "live-job"
    assert restored.status == JobStatus.RUNNING
