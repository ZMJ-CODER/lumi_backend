"""任务事件日志：按 ``seq`` 补拉的持久化事件流（断线续传）。

问题：SSE 只是实时通道，断线期间事件就丢了；第一阶段用 ``JobRunView`` 快照恢复，
但对"断线期间到底发生了什么"没有增量答案。

做法（与 ``operation_events`` / ``capability_events`` 同一机制，不新增第二套事件系统）：

* 复用同一条 Redis list + TTL + 有界裁剪的模式；
* 存的是**标准信封帧**（`app.contracts.events.SseEventEncoder` 的投影结果），
  因此补拉得到的事件与实时流**逐字段一致**（同一个 ``event_id`` / ``seq``）；
* 读取按 ``after_seq`` 过滤（而不是 Redis 游标），因为前端拿到的是 ``seq``；
* Redis 不可用时静默降级（只记日志）：实时流不受影响，补拉返回空。

写入策略：``FrameRecorder`` 在内存里攒帧，非 ``text_delta`` 帧立即 flush、
``text_delta`` 攒到阈值再 flush —— 避免每个正文增量一次 Redis 往返。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

_EVENT_LOG_PREFIX = "job_events:"
_TTL_SECONDS = 3600
_MAX_EVENTS_PER_JOB = 800
#: 正文增量攒批阈值（非正文帧会立即 flush，保证过程事件实时可补拉）。
_DELTA_FLUSH_THRESHOLD = 25

#: 不落日志的事件类型（纯传输态，恢复无意义且量大）。
SKIP_EVENT_TYPES: frozenset[str] = frozenset({"connected", "ping", "pong"})


def _key(job_id: str) -> str:
    return f"{_EVENT_LOG_PREFIX}{job_id}"


async def record_frames(frames: list[dict[str, Any]]) -> int:
    """把若干标准帧写入任务事件日志（按 ``job_id`` 分组、有界、带 TTL）。"""
    grouped: dict[str, list[str]] = {}
    for frame in frames or []:
        if not isinstance(frame, dict):
            continue
        event_type = str(frame.get("type") or "")
        if event_type in SKIP_EVENT_TYPES:
            continue
        job_id = str(frame.get("job_id") or "")
        if not job_id:
            # 没有 job 的流（普通闲聊）不落日志：恢复靠消息历史。
            continue
        try:
            encoded = json.dumps(frame, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            continue
        grouped.setdefault(job_id, []).append(encoded)
    if not grouped:
        return 0
    written = 0
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        for job_id, rows in grouped.items():
            key = _key(job_id)
            await redis.rpush(key, *rows)
            await redis.ltrim(key, -_MAX_EVENTS_PER_JOB, -1)
            await redis.expire(key, _TTL_SECONDS)
            written += len(rows)
    except Exception as exc:  # noqa: BLE001 - 日志不可用不能影响实时流
        logger.debug("[job-event-log] 写入失败（降级，不影响实时流）: {}", str(exc)[:120])
        return 0
    return written


async def read_frames(job_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    """按 ``after_seq`` 读取任务事件（``seq`` 严格大于给定值，保持原有顺序）。"""
    target = str(job_id or "")
    if not target:
        return []
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        rows = await redis.lrange(_key(target), 0, -1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[job-event-log] 读取失败（降级）: {}", str(exc)[:120])
        return []
    out: list[dict[str, Any]] = []
    floor = max(0, int(after_seq or 0))
    for raw in rows or []:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(frame, dict):
            continue
        try:
            seq = int(frame.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq <= floor:
            continue
        out.append(frame)
        if len(out) >= max(1, int(limit or 500)):
            break
    return out


def last_seq_of(frames: list[dict[str, Any]]) -> int:
    """一帧列表里的最大 ``seq``（没有时返回 0）。"""
    best = 0
    for frame in frames or []:
        try:
            best = max(best, int(frame.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return best


class FrameRecorder:
    """攒批写日志的小工具（每个 SSE 流一个实例）。

    用法：``recorder.add(frame)`` 之后由调用方决定何时 ``await recorder.flush()``；
    流结束时必须 flush 一次，避免尾帧丢失。
    """

    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer: list[dict[str, Any]] = []

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def add(self, frame: dict[str, Any]) -> bool:
        """入队一帧；返回 ``True`` 表示"应当立即 flush"（非正文帧或攒批已满）。"""
        if not isinstance(frame, dict):
            return False
        self._buffer.append(frame)
        event_type = str(frame.get("type") or "")
        return event_type != "text_delta" or len(self._buffer) >= _DELTA_FLUSH_THRESHOLD

    async def flush(self) -> int:
        if not self._buffer:
            return 0
        pending, self._buffer = self._buffer, []
        return await record_frames(pending)


__all__ = [
    "FrameRecorder",
    "SKIP_EVENT_TYPES",
    "last_seq_of",
    "read_frames",
    "record_frames",
]
