import asyncio

from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import InMemoryStateStore
from app.agents.orchestration.submission_guard import SubmissionGuard


def test_submission_guard_serializes_and_returns_created_job():
    async def scenario():
        store = InMemoryStateStore()
        guard = SubmissionGuard(store=store)
        calls = []

        async def create(token):
            calls.append(token)
            job = Job(
                job_id="j1", user_id="u1", request="x",
                submission_key="same", status=JobStatus.RUNNING,
            )
            await store.create_job(job)
            return job

        first = await guard.submit(
            user_id="u1", conversation_id=None, submission_key="same", create_job=create,
        )
        second = await guard.submit(
            user_id="u1", conversation_id=None, submission_key="same", create_job=create,
        )
        assert first.job_id == second.job_id
        assert len(calls) == 1

    asyncio.run(scenario())
