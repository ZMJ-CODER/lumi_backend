import asyncio

from app.agents.orchestration.job_error_service import JobErrorService
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import InMemoryStateStore


def test_job_error_service_converges_failure_and_preserves_error_code():
    async def scenario():
        store = InMemoryStateStore()
        job = Job(job_id="j1", user_id="u1", request="x", status=JobStatus.RUNNING)
        await store.create_job(job)
        service = JobErrorService(store=store)

        result = await service.fail("j1", RuntimeError("worker failed"), error_code="WORKER_ERROR")
        assert result is not None
        assert result.status == JobStatus.FAILED
        assert result.error == "worker failed"
        assert result.routing["error_code"] == "WORKER_ERROR"

    asyncio.run(scenario())


def test_job_error_service_does_not_overwrite_completed_job():
    async def scenario():
        store = InMemoryStateStore()
        job = Job(job_id="j2", user_id="u1", request="x", status=JobStatus.COMPLETED)
        await store.create_job(job)
        service = JobErrorService(store=store)

        result = await service.fail("j2", "late failure")
        assert result is not None and result.status == JobStatus.COMPLETED
        assert result.error is None

    asyncio.run(scenario())
