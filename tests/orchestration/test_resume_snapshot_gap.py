"""SSE 快照真空期：断线恢复必须"读快照 + 只补增量"（方案 §6）。

关键断言是**性能契约**：恢复路径不允许重放历史、不允许重建视图——
因此测试直接检查"快照缺失时不会遍历全量日志"，以及"快照覆盖过的事件不再返回"。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import job_event_log
from app.services import resume_snapshot


def _frame(seq: int, *, kind: str = "text_delta", job_id: str = "job-1") -> dict[str, Any]:
    return {"type": kind, "job_id": job_id, "seq": seq, "payload": {"text": f"t{seq}"}}


class _FakeRedis:
    """只实现 job_event_log 用到的 list 操作。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        rows = self.lists.get(key, [])
        self.lists[key] = rows[start:] if stop == -1 else rows[start : stop + 1]
        return True

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        rows = self.lists.get(key, [])
        if stop == -1:
            return rows[start:]
        return rows[start : stop + 1]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.lists.setdefault(f"kv:{key}", [])
        self.lists[f"kv:{key}"] = [value]
        return True

    async def get(self, key: str) -> str | None:
        rows = self.lists.get(f"kv:{key}")
        return rows[0] if rows else None


@pytest.fixture()
def fake_redis(monkeypatch):
    """把 ``get_redis`` 换成替身（事件日志与快照都走它）。"""
    redis = _FakeRedis()
    import app.core.redis as core_redis

    monkeypatch.setattr(core_redis, "get_redis", lambda: redis)
    return redis


@pytest.mark.asyncio
async def test_head_seq_reads_tail_only(fake_redis, monkeypatch):
    """水位读取只扫尾部：恢复路径必须是 O(尾批)，不能 O(全量)。"""
    scanned: list[int] = []
    original = fake_redis.lrange

    async def traced(key, start, stop):
        scanned.append(start)
        return await original(key, start, stop)

    monkeypatch.setattr(fake_redis, "lrange", traced)
    for seq in range(1, 200):
        await job_event_log.record_frames([_frame(seq)])
    head = await job_event_log.head_seq("job-1")
    assert head == 199
    # 只读了一次，且起点是负数（尾部），不是 0（全量）。
    assert len(scanned) == 1
    assert scanned[0] < 0, "水位读取必须只看尾部"


@pytest.mark.asyncio
async def test_recovery_returns_snapshot_plus_only_the_delta(fake_redis, monkeypatch):
    """快照已覆盖的事件**不再返回**（否则客户端还要去重，纯浪费带宽）。"""
    for seq in range(1, 11):
        await job_event_log.record_frames([_frame(seq)])

    async def fake_read_payload(job_id: str):
        return ({"job_id": job_id, "status": "running", "last_seq": 7, "steps": []}, 7)

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", fake_read_payload)
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=3)
    assert pack["resume_mode"] == "snapshot_delta"
    assert pack["snapshot"]["last_seq"] == 7
    assert pack["baseline_seq"] == 7, "起点必须取 max(客户端水位, 快照水位)"
    assert [item["seq"] for item in pack["events"]] == [8, 9, 10]
    assert pack["head_seq"] == 10
    assert pack["truncated"] is False
    assert pack["retry_after_ms"] == 0, "已经追平就不该让客户端空转轮询"


@pytest.mark.asyncio
async def test_recovery_without_snapshot_falls_back_to_events(fake_redis, monkeypatch):
    for seq in range(1, 4):
        await job_event_log.record_frames([_frame(seq)])

    async def no_snapshot(_job_id: str):
        return None, 0

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", no_snapshot)
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=0)
    assert pack["resume_mode"] == "events_only"
    assert pack["snapshot"] is None
    assert [item["seq"] for item in pack["events"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_recovery_with_nothing_asks_for_full_refetch(fake_redis, monkeypatch):
    """快照与事件都不可用 → 明确让客户端走任务详情，而不是返回一个空壳。"""
    async def no_snapshot(_job_id: str):
        return None, 0

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", no_snapshot)
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=0)
    assert pack["resume_mode"] == "full_refetch"
    assert pack["retry_after_ms"] > 0
    assert "任务详情" in pack["message"]


@pytest.mark.asyncio
async def test_recovery_reports_truncation_for_large_gap(fake_redis, monkeypatch):
    """增量超过上限时必须明说 truncated，让客户端继续拉而不是以为已经追平。"""
    for seq in range(1, 40):
        await job_event_log.record_frames([_frame(seq)])

    async def no_snapshot(_job_id: str):
        return None, 0

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", no_snapshot)
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=0, limit=10)
    assert pack["count"] == 10
    assert pack["truncated"] is True
    assert pack["retry_after_ms"] > 0


@pytest.mark.asyncio
async def test_recovery_never_raises_on_broken_snapshot(fake_redis, monkeypatch):
    """快照接口抛异常时也必须给出可用恢复包（降级为纯增量）。"""
    for seq in range(1, 3):
        await job_event_log.record_frames([_frame(seq)])

    async def boom(_job_id: str):
        raise RuntimeError("snapshot store down")

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", boom)
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=0)
    assert pack["resume_mode"] == "events_only"
    assert pack["count"] == 2


@pytest.mark.asyncio
async def test_stale_snapshot_must_not_overwrite_newer_client_state(fake_redis, monkeypatch):
    """**旧快照不得覆盖新状态**（评审 P0）。

    客户端已经到 seq=10，服务端快照只有 seq=5：套用快照会把界面回退到几秒前。
    协议必须明确 ``snapshot_applicable=false``，前端据此只追加增量、保留本地视图。
    """
    for seq in range(1, 13):
        await job_event_log.record_frames([_frame(seq)])

    async def stale_snapshot(job_id: str):
        return ({"job_id": job_id, "status": "running", "last_seq": 5, "steps": []}, 5)

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", stale_snapshot)
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=10)
    assert pack["snapshot_applicable"] is False, "快照落后于客户端水位时不得覆盖"
    assert pack["resume_mode"] != "snapshot_only"
    assert pack["baseline_seq"] == 10, "增量起点以客户端水位为准"
    assert [item["seq"] for item in pack["events"]] == [11, 12]


@pytest.mark.asyncio
async def test_fresh_snapshot_is_applicable(fake_redis, monkeypatch):
    async def fresh_snapshot(job_id: str):
        return ({"job_id": job_id, "status": "running", "last_seq": 9, "steps": []}, 9)

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", fresh_snapshot)
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=4)
    assert pack["snapshot_applicable"] is True


@pytest.mark.asyncio
async def test_event_log_read_failure_is_not_reported_as_caught_up(fake_redis, monkeypatch):
    """**读不到日志 ≠ 没有新事件**（评审 P0）。

    旧实现里 ``read_frames`` 读失败返回 ``[]``，恢复接口就返回
    ``snapshot_only / truncated=false / retry_after_ms=0``，前端以为追平了。
    """
    async def no_snapshot(_job_id: str):
        return None, 0

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", no_snapshot)

    class _BrokenRedis:
        async def lrange(self, *_args, **_kwargs):
            raise RuntimeError("redis down")

    import app.core.redis as core_redis

    monkeypatch.setattr(core_redis, "get_redis", lambda: _BrokenRedis())
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=0)
    assert pack["event_log_available"] is False
    assert pack["resume_mode"] == "full_refetch"
    assert pack["retry_after_ms"] > 0, "读失败必须让客户端稍后重试，而不是以为追平"
    assert "无法确认是否已追平" in pack["message"]


@pytest.mark.asyncio
async def test_snapshot_with_unreadable_log_does_not_claim_caught_up(fake_redis, monkeypatch):
    """有快照但日志读不到：也不能报 snapshot_only（那等于说"已追平"）。"""
    async def fresh_snapshot(job_id: str):
        return ({"job_id": job_id, "status": "running", "last_seq": 3, "steps": []}, 3)

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", fresh_snapshot)

    class _BrokenRedis:
        async def lrange(self, *_args, **_kwargs):
            raise RuntimeError("redis down")

    import app.core.redis as core_redis

    monkeypatch.setattr(core_redis, "get_redis", lambda: _BrokenRedis())
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=1)
    assert pack["event_log_available"] is False
    assert pack["resume_mode"] == "snapshot_delta"
    assert pack["retry_after_ms"] > 0


@pytest.mark.asyncio
async def test_trimmed_log_reports_a_real_gap(fake_redis, monkeypatch):
    """日志被 ltrim 裁掉后，客户端水位与日志起点之间的空档必须报 ``gap_detected``。"""
    async def no_snapshot(_job_id: str):
        return None, 0

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", no_snapshot)
    # 只保留 seq 50 之后的事件（模拟有界裁剪）。
    for seq in range(50, 56):
        await job_event_log.record_frames([_frame(seq)])

    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=10)
    assert pack["oldest_seq"] == 50
    assert pack["gap_detected"] is True, "10→50 之间的事件已经不存在，必须如实报告缺口"
    assert pack["retry_after_ms"] > 0
    assert pack["event_log_available"] is True


@pytest.mark.asyncio
async def test_contiguous_log_reports_no_gap(fake_redis, monkeypatch):
    async def no_snapshot(_job_id: str):
        return None, 0

    monkeypatch.setattr("app.services.job_snapshot_store.read_snapshot_payload", no_snapshot)
    for seq in range(1, 5):
        await job_event_log.record_frames([_frame(seq)])
    pack = await resume_snapshot.build_gap_recovery("job-1", after_seq=0)
    assert pack["oldest_seq"] == 1
    assert pack["gap_detected"] is False
    assert pack["retry_after_ms"] == 0, "无缺口且无截断 = 已追平，不该让客户端空转"
