import asyncio

from app.agents.orchestration.job_finalizer import JobFinalizer
from app.agents.orchestration.models import Job, JobStatus


def test_job_finalizer_ignores_non_terminal_jobs():
    async def scenario():
        events = []

        async def callback(_job):
            events.append("callback")

        finalizer = JobFinalizer(
            stop_heartbeat=lambda _job_id: callback(None),
            on_summary=callback,
            on_task_index=callback,
            on_metric=callback,
            on_learning=callback,
            on_terminal=lambda _job: events.append("terminal"),
            release_capacity=callback,
        )

        assert await finalizer.finalize(Job(job_id="j1", user_id="u1", request="test")) is False
        assert events == []

    asyncio.run(scenario())


def test_job_finalizer_runs_terminal_hooks_in_order():
    async def scenario():
        events = []

        async def hook(name):
            async def callback(_job):
                events.append(name)

            return callback

        finalizer = JobFinalizer(
            stop_heartbeat=await hook("heartbeat"),
            on_summary=await hook("summary"),
            on_task_index=await hook("index"),
            on_metric=await hook("metric"),
            on_learning=await hook("learning"),
            release_capacity=await hook("release"),
            on_terminal=lambda _job: events.append("terminal"),
        )

        job = Job(job_id="j2", user_id="u1", request="test", status=JobStatus.FAILED)
        assert await finalizer.finalize(job) is True
        assert events == ["summary", "index", "metric", "learning", "release", "heartbeat", "terminal"]

    asyncio.run(scenario())
