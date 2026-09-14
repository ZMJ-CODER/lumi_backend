"""全局 RAG 配置：Redis 动态覆盖（管理员可改），.env 兜底."""

import json

from app.core.config import settings
from app.core.redis import get_redis

RAG_CONFIG_KEY = "config:rag"


async def get_rag_overrides() -> dict:
    """读取 Redis 覆盖配置；未配置或异常返回空 dict."""
    try:
        r = get_redis()
        raw = await r.get(RAG_CONFIG_KEY)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


async def set_rag_overrides(cfg: dict) -> None:
    r = get_redis()
    await r.set(RAG_CONFIG_KEY, json.dumps(cfg, ensure_ascii=False))


async def reset_rag_overrides() -> None:
    try:
        r = get_redis()
        await r.delete(RAG_CONFIG_KEY)
    except Exception:  # noqa: BLE001
        pass


async def effective_top_k(default: int | None = None) -> int:
    ov = await get_rag_overrides()
    if ov.get("top_k") is not None:
        return int(ov["top_k"])
    return default or settings.RAG_TOP_K


async def effective_threshold(default: float | None = None) -> float:
    ov = await get_rag_overrides()
    if ov.get("similarity_threshold") is not None:
        return float(ov["similarity_threshold"])
    return default if default is not None else settings.RAG_SIMILARITY_THRESHOLD
