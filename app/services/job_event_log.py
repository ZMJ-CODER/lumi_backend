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
    """把若干标准帧写入任务事件日志（按 ``job_id`` 分组、有界、带 TTL）。

    写之前先过**终态封印**（:mod:`app.services.job_event_seal`）：任务已定局时，
    迟到的内容类帧不再收录——否则补拉接口会把"取消后又冒出来的 step/text"再放给前端，
    前端状态就会回跳。本批出现终态帧时顺手把任务封上（之后不再需要查状态）。
    """
    from app.services.job_event_seal import (
        filter_frames_for_job,
        is_terminal_frame,
        seal_job,
        frame_state,
    )

    cleaned = [dict(frame) for frame in frames or [] if isinstance(frame, dict)]
    if not cleaned:
        return 0
    cleaned, _dropped = await filter_frames_for_job(cleaned)
    if not cleaned:
        return 0

    grouped: dict[str, list[str]] = {}
    terminal_states: dict[str, str] = {}
    for frame in cleaned:
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
        if is_terminal_frame(frame):
            terminal_states[job_id] = frame_state(frame) or "completed"
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
    # 落盘之后再封印：终态帧本身必须已经写进去。
    for job_id, state in terminal_states.items():
        await seal_job(job_id, state)
    return written


async def read_frames(job_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    """按 ``after_seq`` 读取任务事件（``seq`` 严格大于给定值，保持原有顺序）。

    **读失败返回空列表**（与"确实没有新事件"无法区分），因此恢复协议不得只看这个
    返回值：必须配合 :func:`event_log_state` 判断是"读不到"还是"真没有"。
    """
    frames, _state = await read_frames_with_state(job_id, after_seq=after_seq, limit=limit)
    return frames


async def read_frames_with_state(
    job_id: str,
    *,
    after_seq: int = 0,
    limit: int = 500,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读增量并**如实报告日志状态**。

    返回 ``(frames, state)``，``state`` 字段：

    * ``available``：日志可读（``False`` = Redis 读失败/降级，此时 ``frames`` 必然为空，
      **不能**解释成"已追平"）；
    * ``oldest_seq``：日志里**最小**的 ``seq``（被 ``ltrim`` 裁剪后的起点）。客户端水位
      小于它就意味着中间那段事件已经不存在了，必须如实报缺口；
    * ``head_seq``：最大 ``seq``；
    * ``count``：日志总条数。

    为什么必须分开：评审指出的 P0 —— ``read_frames`` 读失败返回 ``[]``，恢复接口于是
    返回 ``snapshot_only / truncated=false / retry_after_ms=0``，前端以为追平了，
    实际上日志根本没读到。
    """
    target = str(job_id or "")
    if not target:
        return [], {"available": False, "oldest_seq": 0, "head_seq": 0, "count": 0}
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        rows = await redis.lrange(_key(target), 0, -1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[job-event-log] 读取失败（降级）: {}", str(exc)[:120])
        return [], {"available": False, "oldest_seq": 0, "head_seq": 0, "count": 0}

    out: list[dict[str, Any]] = []
    floor = max(0, int(after_seq or 0))
    cap = max(1, int(limit or 500))
    oldest = 0
    head = 0
    total = 0
    for raw in rows or []:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(frame, dict):
            continue
        total += 1
        try:
            seq = int(frame.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq:
            oldest = seq if not oldest else min(oldest, seq)
            head = max(head, seq)
        if seq <= floor or len(out) >= cap:
            continue
        out.append(frame)
    return out, {"available": True, "oldest_seq": oldest, "head_seq": head, "count": total}


def last_seq_of(frames: list[dict[str, Any]]) -> int:
    """一帧列表里的最大 ``seq``（没有时返回 0）。"""
    best = 0
    for frame in frames or []:
        try:
            best = max(best, int(frame.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return best


async def head_seq(job_id: str) -> int:
    """事件日志的**当前水位**（末尾帧的 ``seq``）。

    恢复协议需要它回答两个问题："补拉是否已经到底"与"快照水位落后多少"。
    实现上只读列表末尾若干条而不是全量：日志有上界（``_MAX_EVENTS_PER_JOB``），
    但恢复路径必须保持 O(尾批) 而不是 O(全量)，否则丢包重连会拖慢整个接口。
    """
    target = str(job_id or "")
    if not target:
        return 0
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        tail = await redis.lrange(_key(target), -_HEAD_SCAN_LIMIT, -1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[job-event-log] 水位读取失败（降级）: {}", str(exc)[:120])
        return 0
    best = 0
    for raw in tail or []:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(frame, dict):
            try:
                best = max(best, int(frame.get("seq") or 0))
            except (TypeError, ValueError):
                continue
    return best


#: 读水位时只看末尾这么多条（seq 单调递增，找到最大即可）。
_HEAD_SCAN_LIMIT = 50


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
    "head_seq",
    "last_seq_of",
    "read_frames",
    "read_frames_with_state",
    "record_frames",
]
