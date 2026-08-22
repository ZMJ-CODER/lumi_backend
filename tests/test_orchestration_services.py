import asyncio

from app.agents.orchestration.memory_service import OfficeMemoryService
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.query_service import JobQueryService
from app.agents.orchestration.state import InMemoryStateStore


class FakeRedis:
    def __init__(self):
        self.values = {"conv:office:sum:c1": ["旧任务"]}
        self.writes = []

    async def lrange(self, key, _start, _end):
        return self.values.get(key, [])

    async def exists(self, _key):
        return False

    async def rpush(self, key, value):
        self.writes.append(("rpush", key, value))

    async def ltrim(self, key, start, end):
        self.writes.append(("ltrim", key, start, end))

    async def setex(self, key, ttl, value):
        self.writes.append(("setex", key, ttl, value))


def test_office_memory_service_keeps_summary_storage_out_of_orchestrator(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: redis)
    service = OfficeMemoryService()

    assert asyncio.run(service.load_summaries("c1")) == "1. 旧任务"
    job = Job(
        job_id="j1",
        user_id="u1",
        conversation_id="c1",
        request="整理文件",
        scene="office",
        status=JobStatus.COMPLETED,
        result={"answer": "已完成"},
    )
    asyncio.run(service.record_summary(job))
    assert any(item[0] == "rpush" for item in redis.writes)


def test_query_service_owns_read_side_terminal_cleanup():
    store = InMemoryStateStore()
    job = Job(job_id="j1", user_id="u1", request="test", status=JobStatus.COMPLETED)
    asyncio.run(store.create_job(job))
    events = []

    async def no_temporal():
        return False

    async def callback(_job):
        events.append("callback")

    service = JobQueryService(
        store=store,
        live_jobs={},
        probe_temporal=no_temporal,
        stop_heartbeat=callback,
        on_summary=callback,
        on_task_index=callback,
        on_metric=callback,
        on_learning=callback,
        attach_progress=lambda current: callback(current),
        on_terminal=lambda _job: events.append("terminal"),
    )
    # attach_progress is async in production; use a concrete replacement for
    # this focused service contract test.
    async def attach(current):
        return current

    service._attach_progress = attach
    result = asyncio.run(service.get_job("j1"))
    assert result is not None and result.status == JobStatus.COMPLETED
    assert "terminal" in events
