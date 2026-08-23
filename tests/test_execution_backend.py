import asyncio

from app.agents.orchestration.execution_backend import LegacyDagBackend, TemporalStaticBackend
from app.agents.orchestration.models import Job, JobStatus, ResourceClaim, TaskNode, TaskStatus
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.state import InMemoryStateStore


def test_legacy_backend_persists_job_and_starts_runner():
    async def scenario():
        store = InMemoryStateStore()
        live_jobs = {}
        tasks = {}
        api_keys = {}
        started = []

        async def run_job(job_id):
            started.append(job_id)

        backend = LegacyDagBackend(
            store=store,
            live_jobs=live_jobs,
            tasks=tasks,
            api_keys=api_keys,
            run_job=run_job,
        )
        job = Job(job_id="j1", user_id="u1", request="test")

        await backend.submit(job, "secret")
        await tasks["j1"]

        assert (await store.get_job("j1")).job_id == job.job_id
        assert live_jobs["j1"] is job
        assert api_keys == {"j1": "secret"}
        assert started == ["j1"]

    asyncio.run(scenario())


def test_legacy_backend_controls_preserve_lifecycle_semantics():
    async def scenario():
        store = InMemoryStateStore()
        live_jobs = {}
        tasks = {}
        started = []

        async def run_job(job_id):
            started.append(job_id)

        backend = LegacyDagBackend(
            store=store,
            live_jobs=live_jobs,
            tasks=tasks,
            api_keys={},
            run_job=run_job,
        )
        job = Job(
            job_id="j2",
            user_id="u1",
            request="test",
            status=JobStatus.RUNNING,
            nodes=[TaskNode(id="n1", agent="worker", status=TaskStatus.PENDING)],
        )
        await store.create_job(job)

        paused = await backend.pause(await store.get_job("j2"))
        assert paused.job.status == JobStatus.PAUSED
        resumed = await backend.resume(await store.get_job("j2"))
        await tasks["j2"]
        assert resumed.job.status == JobStatus.RUNNING
        assert started == ["j2"]

        cancelled = await backend.cancel(await store.get_job("j2"), keep_completed=False)
        assert cancelled.release_capacity is True
        assert cancelled.job.status == JobStatus.CANCELLED
        assert cancelled.job.nodes[0].status == TaskStatus.CANCELLED

    asyncio.run(scenario())


def test_temporal_static_candidate_gate_keeps_dynamic_and_write_jobs_legacy():
    candidate = Job(
        job_id="static-read",
        user_id="u1",
        request="summarize",
        nodes=[TaskNode(id="read", agent="retrieval")],
    )
    assert RuntimeGateway.can_run_static(candidate) is True

    assert RuntimeGateway.can_run_static(Job(
        job_id="react", user_id="u1", request="explore",
        nodes=[TaskNode(id="react", agent="react_step")],
    )) is False
    assert RuntimeGateway.can_run_static(Job(
        job_id="approval", user_id="u1", request="send",
        nodes=[TaskNode(id="send", agent="direct_llm", approval=True)],
    )) is False
    assert RuntimeGateway.can_run_static(Job(
        job_id="write", user_id="u1", request="write",
        nodes=[TaskNode(
            id="write", agent="direct_llm",
            resource_claims=[ResourceClaim(key="doc:1", mode="write")],
        )],
    )) is False


def test_temporal_static_submission_persists_bootstrap_and_frozen_llm_config(monkeypatch):
    async def scenario():
        store = InMemoryStateStore()
        runtime = RuntimeGateway(store=store, temporal_mode=True)
        captured = {}

        async def store_config(job_id, config):
            captured["config"] = (job_id, config)

        async def start_workflow(payload, job_id):
            captured["workflow"] = (payload, job_id)

        monkeypatch.setattr(
            "app.agents.orchestration.temporal.client.store_job_llm_config", store_config,
        )
        monkeypatch.setattr(
            "app.agents.orchestration.temporal.client.start_agent_workflow", start_workflow,
        )
        job = Job(
            job_id="static-submit",
            user_id="u1",
            request="summarize",
            nodes=[TaskNode(id="read", agent="retrieval")],
        )

        await runtime.submit_static(job, None, {"api_key": "key", "model": "m"})

        assert job.routing["runtime"] == "temporal_static"
        assert (await store.get_job(job.job_id)).routing["runtime"] == "temporal_static"
        assert captured["config"] == (job.job_id, {"api_key": "key", "model": "m"})
        assert captured["workflow"][0]["config"]["node_concurrency"] > 0

    asyncio.run(scenario())


def test_temporal_static_control_does_not_fall_back_to_legacy_when_worker_is_unavailable():
    async def scenario():
        store = InMemoryStateStore()
        runtime = RuntimeGateway(store=store, temporal_mode=False)
        backend = TemporalStaticBackend(runtime)
        job = Job(
            job_id="static-control",
            user_id="u1",
            request="summarize",
            status=JobStatus.RUNNING,
            routing={"runtime": "temporal_static"},
            nodes=[TaskNode(id="read", agent="retrieval")],
        )

        result = await backend.cancel(job)

        assert result is not None
        assert result.handled is True
        assert result.release_capacity is False
        assert result.error and "未送达" in result.error
        assert job.status == JobStatus.RUNNING

    asyncio.run(scenario())
