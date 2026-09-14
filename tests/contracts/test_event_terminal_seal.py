"""终态封印（方案 §6.2）契约回归：取消后迟到事件必须被吞掉，状态不回跳。

后端两道闸门都要测：

1. **流内封印** ``StreamSeal``：同一条流出现终态帧后，后续内容类帧不再外发，
   元数据帧（``title`` / ``summary``）照常放行；
2. **任务级封印**（Redis ``job_seal:{job_id}``）：``cancel_job`` 受理即封印，
   跨流/跨进程/补拉同样生效——``record_frames`` 不再收录迟到内容帧，
   因此 ``GET /agents/jobs/{id}/events?after_seq=`` 补拉也拿不到。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.job_event_log import FrameRecorder, read_frames, record_frames
from app.services.job_event_seal import (
    StreamSeal,
    filter_frames_for_job,
    is_seal_blocked,
    is_terminal_frame,
    job_seal_state,
    seal_job,
    unseal_job,
)


class _FakeRedis:
    """最小 Redis 替身：字符串 + 列表 + TTL（封印与事件日志都要用）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        if ex is not None:
            self.expires[key] = int(ex)
        return True

    async def delete(self, *keys: str):
        for key in keys:
            self.store.pop(key, None)
            self.lists.pop(key, None)
        return len(keys)

    async def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        rows = self.lists.get(key, [])
        self.lists[key] = rows[start:] if end == -1 else rows[start : end + 1]
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = int(seconds)
        return True

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        rows = self.lists.get(key, [])
        return rows[start:] if end == -1 else rows[start : end + 1]


def _install(monkeypatch) -> _FakeRedis:
    import app.core.redis as redis_module

    fake = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake)
    return fake


def _frame(event_type: str, **extra: Any) -> dict[str, Any]:
    return {"type": event_type, "job_id": "job-1", "seq": extra.pop("seq", 1), **extra}


# ── 1. 判据 ──────────────────────────────────────────────


def test_terminal_frame_detection():
    assert is_terminal_frame(_frame("done"))
    assert is_terminal_frame(_frame("task_completed"))
    assert is_terminal_frame(_frame("task_failed"))
    assert is_terminal_frame(_frame("cancelled"))
    assert is_terminal_frame(_frame("control", payload={"state": "completed"}))
    assert is_terminal_frame(_frame("control", payload={"state": "cancelled"}))
    assert is_terminal_frame(_frame("control", state="failed")), "旧投影 state 写在顶层"
    assert not is_terminal_frame(_frame("control", payload={"state": "running"}))
    assert not is_terminal_frame(_frame("control", payload={"state": "waiting_approval"}))
    assert not is_terminal_frame(_frame("text_delta"))
    assert not is_terminal_frame(_frame("step_completed"))


def test_only_content_events_are_blocked_after_terminal():
    for event_type in (
        "text_delta", "delta", "process", "step_started", "step_completed",
        "tool_started", "artifact_created", "view_updated", "approval_required",
        "capability_started", "operation_completed",
    ):
        assert is_seal_blocked(_frame(event_type)), event_type
    for event_type in ("title", "summary", "audio_ready", "job", "task_router", "usage"):
        assert not is_seal_blocked(_frame(event_type)), event_type


# ── 2. 流内封印 ──────────────────────────────────────────


def test_stream_seal_drops_late_content_but_keeps_metadata():
    seal = StreamSeal()
    frames = [
        _frame("text_delta", seq=1),
        _frame("step_completed", seq=2),
        _frame("control", payload={"state": "completed"}, seq=3),
        _frame("text_delta", seq=4),          # 迟到
        _frame("step_completed", seq=5),      # 迟到
        _frame("title", seq=6),               # 元数据：放行
    ]
    kept, dropped = seal.filter(frames)
    assert [frame["type"] for frame in kept] == ["text_delta", "step_completed", "control", "title"]
    assert dropped == 2
    assert seal.sealed and seal.state == "completed"
    assert seal.dropped == 2


def test_stream_seal_never_blocks_terminal_frames():
    seal = StreamSeal()
    seal.filter([_frame("done", seq=1)])
    kept, dropped = seal.filter([
        _frame("text_delta", seq=2),
        _frame("control", payload={"state": "cancelled"}, seq=3),
        _frame("done", seq=4),
    ])
    assert [frame["type"] for frame in kept] == ["control", "done"]
    assert dropped == 1


def test_stream_seal_is_noop_before_terminal():
    seal = StreamSeal()
    kept, dropped = seal.filter([_frame("text_delta"), _frame("process")])
    assert len(kept) == 2 and dropped == 0 and not seal.sealed


# ── 3. 任务级封印（Redis） ───────────────────────────────


def test_record_frames_seals_job_on_terminal_and_drops_late_content(monkeypatch):
    redis = _install(monkeypatch)

    async def main():
        first = await record_frames([
            _frame("step_started", seq=1),
            _frame("text_delta", seq=2),
            _frame("done", seq=3),
        ])
        state_after_terminal = await job_seal_state("job-1")
        late = await record_frames([_frame("text_delta", seq=4), _frame("step_completed", seq=5)])
        replay = await read_frames("job-1")
        metadata = await record_frames([_frame("title", seq=6)])
        return first, state_after_terminal, late, replay, metadata

    first, state_after_terminal, late, replay, metadata = asyncio.run(main())
    assert first == 3
    assert state_after_terminal == "completed", "终态帧落盘后必须封印任务"
    assert late == 0, "迟到的内容帧不得写进补拉日志"
    assert [frame["type"] for frame in replay] == ["step_started", "text_delta", "done"]
    assert metadata == 1, "元数据帧（标题）不受封印影响"
    assert redis.lists, "事件日志确实写进了 Redis"


def test_record_frames_respects_cancel_seal_before_any_terminal_frame(monkeypatch):
    """取消受理（封印写入）时，即使本流还没吐终态帧，内容帧也不再收录。"""
    _install(monkeypatch)

    async def main():
        await seal_job("job-1", "cancelled", reason_code="SYSTEM_CANCELLED")
        written = await record_frames([_frame("text_delta", seq=1), _frame("step_completed", seq=2)])
        kept, dropped = await filter_frames_for_job([
            _frame("text_delta", seq=3),
            _frame("control", payload={"state": "cancelled"}, seq=4),
        ])
        state = await job_seal_state("job-1")
        await unseal_job("job-1")
        after = await job_seal_state("job-1")
        return written, kept, dropped, state, after

    written, kept, dropped, state, after = asyncio.run(main())
    assert written == 0, "已取消的任务不得再收录内容帧"
    assert dropped == 1 and [frame["type"] for frame in kept] == ["control"]
    assert state == "cancelled"
    assert after == "", "解除封印后不再拦截（恢复/重跑路径）"


def test_record_frames_degrades_when_redis_is_down(monkeypatch):
    """Redis 不可用：封印查不到就按"未封印"处理，实时流绝不能因此中断。"""

    def boom():
        raise RuntimeError("redis down")

    import app.core.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", boom)

    async def main():
        return await record_frames([_frame("text_delta", seq=1)])

    assert asyncio.run(main()) == 0, "写入降级返回 0，但不抛错"


def test_frame_recorder_flushes_and_then_drops_late_frames(monkeypatch):
    _install(monkeypatch)

    async def main():
        recorder = FrameRecorder()
        recorder.add(_frame("done", seq=1))
        flushed = await recorder.flush()
        recorder.add(_frame("text_delta", seq=2))
        second = await recorder.flush()
        replay = await read_frames("job-1")
        return flushed, second, replay



    flushed, second, replay = asyncio.run(main())
    assert flushed == 1 and second == 0
    assert [frame["type"] for frame in replay] == ["done"]


# ── 4. 取消 / 恢复链路真的会封印 ─────────────────────────


class _StubOperations:
    def __init__(self, job: Any) -> None:
        self._job = job

    async def cancel(self, job_id: str, keep_completed: bool = True):
        return self._job

    async def resume(self, job_id: str):
        return self._job


def test_cancel_job_seals_and_resume_job_unseals(monkeypatch):


    from app.agents.orchestration.orchestrator import AgentOrchestrator

    _install(monkeypatch)
    orchestrator = AgentOrchestrator(temporal_enabled=False)
    monkeypatch.setattr(orchestrator, "_operations", _StubOperations(object()), raising=False)

    async def main():
        await orchestrator.cancel_job("job-1")
        sealed = await job_seal_state("job-1")
        await orchestrator.resume_job("job-1")
        unsealed = await job_seal_state("job-1")
        return sealed, unsealed

    sealed, unsealed = asyncio.run(main())
    assert sealed == "cancelled", "取消受理即定局（方案 §6.2 后端闸门）"
    assert unsealed == ""


def test_cancel_job_without_job_does_not_seal(monkeypatch):


    from app.agents.orchestration.orchestrator import AgentOrchestrator

    _install(monkeypatch)
    orchestrator = AgentOrchestrator(temporal_enabled=False)
    monkeypatch.setattr(orchestrator, "_operations", _StubOperations(None), raising=False)

    async def main():
        result = await orchestrator.cancel_job("job-404")
        return result, await job_seal_state("job-404")

    result, state = asyncio.run(main())
    assert result is None
    assert state == "", "任务不存在时不该留下封印"
