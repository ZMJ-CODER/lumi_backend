"""Contract tests for the local-only rolling Temporal manifest runtime.

These tests do not require a Temporal server.  They protect the migration
boundaries that are easiest to accidentally weaken: small workflow payloads,
read-only rollout gating, cancellation ownership, and Redis channel leases.
"""

import asyncio

from app.agents.orchestration.models import Job, JobStatus, TaskNode
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.state import InMemoryStateStore


def _manifest_job(*routes: str, subtasks: bool = False) -> Job:
    return Job(
        job_id="manifest-temporal-test",
        user_id="user-1",
        request="执行任务清单",
        routing={
            "manifest": {
                "items": [
                    {
                        "id": f"item-{index}",
                        "route": route,
                        "subtasks": [{"instruction": "nested"}] if subtasks else [],
                    }
                    for index, route in enumerate(routes, start=1)
                ]
            }
        },
    )


def test_manifest_temporal_gate_allows_only_read_only_flat_routes():
    assert AgentOrchestrator._can_run_manifest_temporal(_manifest_job("direct_llm", "rag"))
    assert not AgentOrchestrator._can_run_manifest_temporal(_manifest_job("deterministic_script"))
    assert not AgentOrchestrator._can_run_manifest_temporal(_manifest_job("agent"))
    assert not AgentOrchestrator._can_run_manifest_temporal(_manifest_job("direct_llm", subtasks=True))
    assert not AgentOrchestrator._can_run_manifest_temporal(Job(
        job_id="not-a-manifest", user_id="user-1", request="普通任务"
    ))


def test_submit_manifest_temporal_persists_job_but_sends_only_runtime_reference(monkeypatch):
    store = InMemoryStateStore()
    orchestrator = AgentOrchestrator(store=store, temporal_enabled=True)
    job = _manifest_job("direct_llm")
    workflow_calls = []
    secret_calls = []

    async def start_manifest(payload, job_id):
        workflow_calls.append((payload, job_id))

    async def store_secret(job_id, api_key):
        secret_calls.append((job_id, api_key))

    monkeypatch.setattr(
        "app.agents.orchestration.temporal.client.start_manifest_workflow", start_manifest
    )
    monkeypatch.setattr("app.agents.orchestration.temporal.client.store_byok_key", store_secret)

    async def scenario():
        await orchestrator._submit_manifest_temporal(job, "secret-not-in-history")
        return await store.get_job(job.job_id)

    stored = asyncio.run(scenario())
    assert stored is not None
    assert stored.routing["runtime"] == "manifest_temporal"
    assert secret_calls == [(job.job_id, "secret-not-in-history")]
    assert len(workflow_calls) == 1
    payload, workflow_id = workflow_calls[0]
    assert workflow_id == job.job_id
    assert payload["job_id"] == job.job_id
    assert set(payload) == {
        "job_id", "heartbeat_seconds", "batch_timeout_seconds", "continue_after_batches"
    }
    assert "secret-not-in-history" not in repr(payload)
    assert "执行任务清单" not in repr(payload)


def test_start_manifest_workflow_uses_dedicated_task_queue(monkeypatch):
    from app.agents.orchestration.temporal import client as temporal_client
    from app.core.config import settings

    calls = []

    class FakeClient:
        async def start_workflow(self, workflow_type, payload, **kwargs):
            calls.append((workflow_type, payload, kwargs))

    async def get_client():
        return FakeClient()

    monkeypatch.setattr(temporal_client, "get_temporal_client", get_client)
    asyncio.run(temporal_client.start_manifest_workflow({"job_id": "j1"}, "j1"))
    assert len(calls) == 1
    assert calls[0][1] == {"job_id": "j1"}
    assert calls[0][2]["task_queue"] == settings.TEMPORAL_MANIFEST_TASK_QUEUE


def test_manifest_activity_does_not_run_a_terminal_job(monkeypatch):
    from app.agents.orchestration.temporal import manifest_activities

    terminal = Job(
        job_id="done", user_id="user-1", request="任务", status=JobStatus.CANCELLED,
    )

    class FakeStore:
        async def get_job(self, job_id):
            assert job_id == "done"
            return terminal

    monkeypatch.setattr(manifest_activities, "RedisStateStore", lambda: FakeStore())
    result = asyncio.run(manifest_activities.run_manifest_batch_activity({"job_id": "done"}))
    assert result == {"terminal": True, "status": "cancelled"}


def test_channel_limiter_renews_redis_slot_while_task_is_active(monkeypatch):
    from app.agents.orchestration import channel_limits
    import app.core.redis as redis_module

    calls = []

    class FakeRedis:
        async def eval(self, script, _keys, *_args):
            calls.append(script)
            return 1

        async def zrem(self, _key, _token):
            return 1

    monkeypatch.setattr(redis_module, "get_redis", lambda: FakeRedis())
    original_sleep = asyncio.sleep
    sleep_calls = 0

    async def fast_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            await original_sleep(0)
            return
        await original_sleep(3600)

    monkeypatch.setattr(channel_limits.asyncio, "sleep", fast_sleep)
    limiter = channel_limits.ChannelLimiter()

    async def scenario():
        async with limiter.claim("direct_llm", lease_seconds=60):
            await original_sleep(0)
            await original_sleep(0)

    asyncio.run(scenario())
    assert channel_limits._ACQUIRE in calls
    assert channel_limits._RENEW in calls


def test_manifest_activity_stops_before_controller_after_concurrent_cancel(monkeypatch):
    """A late cancel must prevent cursor advancement and final synthesis."""
    from app.agents.orchestration.temporal import manifest_activities
    import app.agents.orchestration.execution.validation as dag_module

    running = Job(
        job_id="cancel-race",
        user_id="user-1",
        request="任务",
        status=JobStatus.RUNNING,
        nodes=[TaskNode(id="n1", name="n1", agent="direct_llm")],
    )

    class FakeStore:
        def __init__(self):
            self.job = running

        async def get_job(self, _job_id):
            return self.job

    store = FakeStore()
    monkeypatch.setattr(manifest_activities, "RedisStateStore", lambda: store)

    async def cancel_during_execution(job, *_args, **_kwargs):
        job.status = JobStatus.CANCELLED
        store.job = job

    async def should_not_continue(self, _job):
        raise AssertionError("cancelled batch must not advance the manifest")

    monkeypatch.setattr(dag_module, "execute_dag", cancel_during_execution)
    monkeypatch.setattr(AgentOrchestrator, "_continue_manifest_job", should_not_continue)

    async def no_byok_key(_job_id):
        return None

    monkeypatch.setattr(
        "app.agents.orchestration.temporal.client.load_byok_key", no_byok_key
    )
    result = asyncio.run(
        manifest_activities.run_manifest_batch_activity({"job_id": "cancel-race"})
    )
    assert result == {"terminal": True, "status": "cancelled"}


def test_manifest_failure_activity_converges_running_job_and_releases_admission(monkeypatch):
    from app.agents.orchestration.temporal import manifest_activities

    running = Job(job_id="failed-batch", user_id="user-1", request="任务", status=JobStatus.RUNNING)

    class FakeStore:
        async def get_job(self, job_id):
            assert job_id == "failed-batch"
            return running

        async def save_job(self, job):
            assert job.status == JobStatus.FAILED

    released = []

    async def release(**kwargs):
        released.append(kwargs)

    monkeypatch.setattr(manifest_activities, "RedisStateStore", lambda: FakeStore())
    monkeypatch.setattr(
        "app.agents.orchestration.admission.job_admission.release", release
    )
    asyncio.run(
        manifest_activities.fail_manifest_job_activity(
            {"job_id": "failed-batch", "error": "activity retry exhausted"}
        )
    )
    assert running.status == JobStatus.FAILED
    assert running.error == "activity retry exhausted"
    assert released == [{"job_id": "failed-batch", "user_id": "user-1"}]
