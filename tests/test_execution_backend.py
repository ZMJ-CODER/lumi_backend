import asyncio

from app.agents.orchestration.execution_backend import LegacyDagBackend
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
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
