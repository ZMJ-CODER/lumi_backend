"""副作用幂等日志：防止节点内部重试和 Temporal Activity 重试重复提交。"""

from __future__ import annotations

import asyncio
import json
import time


_memory: dict[str, dict] = {}
_memory_lock = asyncio.Lock()


def _key(key: str) -> str:
    return f"agent:effect:{key}"


async def _redis():
    try:
        from app.core.redis import get_redis

        return get_redis()
    except Exception:  # noqa: BLE001
        return None


async def get_effect(key: str) -> dict | None:
    redis = await _redis()
    if redis is not None:
        raw = await redis.get(_key(key))
        return json.loads(raw) if raw else None
    async with _memory_lock:
        return dict(_memory[key]) if key in _memory else None


async def begin_effect(key: str) -> tuple[bool, dict | None]:
    record = {"status": "pending", "updated_at": time.time()}
    redis = await _redis()
    if redis is not None:
        created = await redis.set(_key(key), json.dumps(record), ex=86400 * 7, nx=True)
        return bool(created), None if created else await get_effect(key)
    async with _memory_lock:
        if key in _memory:
            return False, dict(_memory[key])
        _memory[key] = record
        return True, None


async def finish_effect(key: str, status: str, result: dict | None = None) -> None:
    record = {"status": status, "result": result, "updated_at": time.time()}
    redis = await _redis()
    if redis is not None:
        await redis.set(_key(key), json.dumps(record, ensure_ascii=False, default=str), ex=86400 * 7)
        return
    async with _memory_lock:
        _memory[key] = record
