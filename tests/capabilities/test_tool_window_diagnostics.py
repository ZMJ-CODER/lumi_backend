"""工具窗口诊断（P1）：按任务读回"四层证据"的端到端回归。

要回答的问题是线上最难问的那个：**模型这次到底拿到了哪些工具，少了哪个，在哪一层少的**。
因此断言分三组：

1. 落盘/读回的语义（最新在前、有上限、失败静默）；
2. 无事件循环时不硬造 loop（诊断代码没有资格决定并发模型）；
3. 接口形状与越权访问（`dropped_core` 非空必须能被一眼看出，别人的任务读不到）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import resume_snapshot


class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}

    async def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        rows = self.lists.get(key, [])
        self.lists[key] = rows[start:] if stop == -1 else rows[start : stop + 1]
        return True

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        rows = self.lists.get(key, [])
        return rows[start:] if stop == -1 else rows[start : stop + 1]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = seconds
        return True


@pytest.fixture()
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    import app.core.redis as core_redis

    monkeypatch.setattr(core_redis, "get_redis", lambda: redis)
    return redis


def _window(scene: str = "chat", *, dropped_core: list[str] | None = None) -> dict:
    return {
        "scene": scene,
        "limit": 8,
        "layers": {"catalog": ["a", "b"], "eligible": ["a", "b"], "ranked": ["a"], "final": ["a"]},
        "counts": {"catalog": 2, "eligible": 2, "ranked": 1, "final": 1},
        "dropped_by_layer": {"ranked": ["b"]},
        "pinned": ["a"],
        "dropped_core": dropped_core or [],
        "trimmed_optional": [],
        "visibility": {"a": "available", "b": "unavailable"},
        "mandatory_reason": "core",
    }


# ── 1. 落盘/读回语义 ────────────────────────────────────


@pytest.mark.asyncio
async def test_windows_are_returned_newest_first(fake_redis):
    for scene in ("first", "second", "third"):
        await resume_snapshot.record_tool_window("job-1", _window(scene))
    rows = await resume_snapshot.read_tool_windows("job-1")
    assert [row["scene"] for row in rows] == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_window_list_is_trimmed_and_has_ttl(fake_redis):
    for index in range(resume_snapshot.TOOL_WINDOW_MAX_ENTRIES + 5):
        await resume_snapshot.record_tool_window("job-1", _window(f"s{index}"))
    key = resume_snapshot.tool_window_key("job-1")
    assert len(fake_redis.lists[key]) == resume_snapshot.TOOL_WINDOW_MAX_ENTRIES
    assert fake_redis.expires[key] == resume_snapshot.TOOL_WINDOW_TTL_SECONDS
    rows = await resume_snapshot.read_tool_windows("job-1", limit=3)
    assert [row["scene"] for row in rows] == ["s24", "s23", "s22"]


@pytest.mark.asyncio
async def test_read_failure_is_silent(monkeypatch):
    """Redis 读失败 → 空列表，不抛错（诊断接口挂了不能连带任务详情挂）。"""
    import app.core.redis as core_redis

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(core_redis, "get_redis", _boom)
    assert await resume_snapshot.read_tool_windows("job-1") == []


@pytest.mark.asyncio
async def test_write_failure_is_silent(monkeypatch):
    import app.core.redis as core_redis

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(core_redis, "get_redis", _boom)
    await resume_snapshot.record_tool_window("job-1", _window())  # 不抛错即可


@pytest.mark.asyncio
async def test_empty_job_or_payload_is_a_noop(fake_redis):
    await resume_snapshot.record_tool_window("", _window())
    await resume_snapshot.record_tool_window("job-1", {})
    assert fake_redis.lists == {}
    assert await resume_snapshot.read_tool_windows("") == []


@pytest.mark.asyncio
async def test_broken_entries_are_skipped(fake_redis):
    await resume_snapshot.record_tool_window("job-1", _window("ok"))
    fake_redis.lists[resume_snapshot.tool_window_key("job-1")].append("{not json")
    rows = await resume_snapshot.read_tool_windows("job-1")
    assert [row["scene"] for row in rows] == ["ok"]


# ── 2. 记录入口（无 loop 不硬造）──────────────────────────


def test_snapshot_recording_without_loop_does_not_explode(fake_redis):
    """同步上下文（无事件循环）里只记日志，不落盘、不报错。"""
    from app.observability.observability import record_tool_window_snapshot

    payload = record_tool_window_snapshot(
        scene="chat", catalog=["a"], eligible=["a"], ranked=["a"], final=["a"], job_id="job-1"
    )
    assert payload["counts"]["final"] == 1
    assert fake_redis.lists == {}, "没有事件循环就不该有后台写入"


def test_snapshot_recording_without_job_id_does_not_touch_redis(fake_redis):
    from app.observability.observability import record_tool_window_snapshot

    record_tool_window_snapshot(scene="chat", final=["a"])
    assert fake_redis.lists == {}


@pytest.mark.asyncio
async def test_snapshot_recording_persists_inside_running_loop(fake_redis):
    from app.observability.observability import record_tool_window_snapshot

    record_tool_window_snapshot(
        scene="chat",
        catalog=["a", "b"],
        eligible=["a"],
        ranked=["a"],
        final=["a"],
        layer="chat_graph.final",
        job_id="job-1",
    )
    for _ in range(5):
        await asyncio.sleep(0)
    rows = await resume_snapshot.read_tool_windows("job-1")
    assert len(rows) == 1
    assert rows[0]["job_id"] == "job-1"
    assert rows[0]["layer"] == "chat_graph.final"
    assert rows[0]["dropped_by_layer"]["catalog→eligible"] == ["b"]


# ── 3. 接口形状与越权 ──────────────────────────────────


class _OrchestratorStub:
    def __init__(self, job) -> None:
        self._job = job

    async def get_job(self, job_id: str):
        if self._job is not None and self._job.job_id == job_id:
            return self._job
        return None


def _stub_job(monkeypatch, *, user_id: str = "u1"):
    from types import SimpleNamespace

    from app.api.v1 import agents as agents_api

    job = SimpleNamespace(job_id="job-1", user_id=user_id)
    monkeypatch.setattr(agents_api, "orchestrator", _OrchestratorStub(job))
    return job


@pytest.mark.asyncio
async def test_endpoint_exposes_layers_and_dropped_core_flag(fake_redis, monkeypatch):
    from app.api.v1 import agents as agents_api

    _stub_job(monkeypatch)
    await resume_snapshot.record_tool_window(
        "job-1", {**_window("chat"), "job_id": "job-1", "dropped_core": ["workspace_navigator"]}
    )
    body = await agents_api.read_agent_job_tool_window("job-1", 20, {"sub": "u1"})
    data = body["data"]
    assert data["available"] is True
    assert data["count"] == 1
    assert data["max_entries"] == resume_snapshot.TOOL_WINDOW_MAX_ENTRIES
    assert data["dropped_core_total"] == 1
    assert data["has_dropped_core"] is True
    window = data["windows"][0]
    assert window["layers"]["final"] == ["a"]
    assert window["visibility"]["b"] == "unavailable"
    assert window["pinned"] == ["a"]


@pytest.mark.asyncio
async def test_endpoint_without_windows_reports_unavailable(fake_redis, monkeypatch):
    from app.api.v1 import agents as agents_api

    _stub_job(monkeypatch)
    body = await agents_api.read_agent_job_tool_window("job-1", 20, {"sub": "u1"})
    assert body["data"]["available"] is False
    assert body["data"]["count"] == 0
    assert body["data"]["has_dropped_core"] is False
    assert "暂无" in body["message"]


@pytest.mark.asyncio
async def test_endpoint_rejects_other_users_job(fake_redis, monkeypatch):
    from app.api.v1 import agents as agents_api
    from app.core.exceptions import NotFoundException

    _stub_job(monkeypatch, user_id="owner")
    with pytest.raises(NotFoundException):
        await agents_api.read_agent_job_tool_window("job-1", 20, {"sub": "someone-else"})


@pytest.mark.asyncio
async def test_endpoint_caps_requested_limit(fake_redis, monkeypatch):
    """请求上限不能超过保留条数（否则前端会以为"只有这么多帧"）。"""
    from app.api.v1 import agents as agents_api

    _stub_job(monkeypatch)
    body = await agents_api.read_agent_job_tool_window("job-1", 1000, {"sub": "u1"})
    assert body["data"]["max_entries"] == resume_snapshot.TOOL_WINDOW_MAX_ENTRIES
