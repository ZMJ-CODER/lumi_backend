import asyncio

from app.agents.orchestration.control_service import JobControlService
from app.agents.orchestration.backends.contracts import BackendControlResult
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import InMemoryStateStore


class _Finalizer:
    def __init__(self):
        self.finalized = []
        self.suspended = []

    async def finalize(self, job):
        self.finalized.append(job.job_id)

    async def suspend_capacity(self, job):
        self.suspended.append(job.job_id)


class _AlreadyTerminalBackend:
    async def cancel(self, job, _keep_completed=True):
        return BackendControlResult(job, release_capacity=False)


class _UnusedBackend:
    async def cancel(self, *_args, **_kwargs):
        raise AssertionError("fallback backend must not be called")


def test_cancel_releases_capacity_when_runner_already_marked_job_terminal():
    async def scenario():
        store = InMemoryStateStore()
        job = Job(job_id="already-cancelled", user_id="user-1", request="test", status=JobStatus.CANCELLED)
        await store.create_job(job)
        finalizer = _Finalizer()
        service = JobControlService(
            repository=store,
            approval=None,
            temporal_backend=_UnusedBackend(),
            static_backend=_AlreadyTerminalBackend(),
            legacy_backend=_UnusedBackend(),
            finalizer=finalizer,
        )

        result = await service.cancel(job.job_id)

        assert result is not None
        assert result.job_id == job.job_id
        assert finalizer.finalized == []
        assert finalizer.suspended == [job.job_id]

    asyncio.run(scenario())
