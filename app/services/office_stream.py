"""Short-lived text stream for office task output.

Node status remains the authoritative job state in Redis.  This module only
buffers presentation deltas so a long writing/summary response can reach the
chat SSE connection before the DAG node finishes.
"""

from __future__ import annotations

import json
import time

from app.core.redis import get_redis

_PREFIX = "office_stream:"
_TTL_SECONDS = 900


def _key(job_id: str) -> str:
    return f"{_PREFIX}{job_id}"


async def push_delta(job_id: str, node_id: str, text: str) -> None:
    if not job_id or not node_id or not text:
        return
    try:
        redis = get_redis()
        await redis.rpush(
            _key(job_id),
            json.dumps({"node_id": node_id, "content": text, "ts": time.time()}, ensure_ascii=False),
        )
        await redis.expire(_key(job_id), _TTL_SECONDS)
    except Exception:
        # Presentation streaming must never fail a completed office task.
        return


async def read_deltas(job_id: str, cursor: int = 0) -> tuple[list[dict], int]:
    try:
        redis = get_redis()
        items = await redis.lrange(_key(job_id), max(0, int(cursor)), -1)
    except Exception:
        return [], max(0, int(cursor))
    events: list[dict] = []
    for raw in items:
        try:
            event = json.loads(raw)
            if isinstance(event, dict) and event.get("content"):
                events.append(event)
        except (TypeError, ValueError):
            continue
    return events, max(0, int(cursor)) + len(items)
