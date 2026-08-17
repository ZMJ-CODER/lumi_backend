"""健康检查：供容器编排（compose healthcheck）与外部探活使用.

高并发注意：健康检查是高频探活路径，不能每次请求都占用数据库连接池槽位
（并发一高就会把连接池打满、拖垮整条请求链路）。
这里把 DB / Redis 探测结果做 TTL 缓存：正常态 5s 一探，异常态 1s 一探，
其余请求直接返回缓存，不碰连接池。
"""

import time

from fastapi import APIRouter

router = APIRouter()

# 探测结果缓存：正常态 5s / 异常态 1s 刷新
_CACHE_TTL_OK = 5.0
_CACHE_TTL_DEGRADED = 1.0
_cache = {"ts": 0.0, "checks": {"database": "unknown", "redis": "unknown"}}


async def _refresh_checks() -> dict:
    checks: dict[str, str] = {}
    try:
        from app.core.database import async_session_factory
        from sqlalchemy import text

        async with async_session_factory() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        checks["database"] = "error"
    try:
        from app.core.redis import get_redis

        r = get_redis()
        await r.ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "error"
    return checks


@router.get("")
async def health():
    """基础健康检查：DB / Redis 连通性（HTTP 始终 200，状态见 data.status）."""
    now = time.monotonic()
    degraded = "error" in _cache["checks"].values()
    interval = _CACHE_TTL_DEGRADED if degraded else _CACHE_TTL_OK
    if now - _cache["ts"] >= interval:
        _cache["checks"] = await _refresh_checks()
        _cache["ts"] = now
    status = "ok" if all(v == "ok" for v in _cache["checks"].values()) else "degraded"
    return {"code": 0, "data": {"status": status, "checks": dict(_cache["checks"])}}
