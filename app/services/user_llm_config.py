"""用户级 LLM 配置（模型选择）—— Redis 存储，按用户隔离.

只存非敏感选择：provider / model / reasoning_effort / byok 标记。
BYOK 的 API key 绝不落服务端：本地加密存储 + 每次请求临时携带（X-LLM-API-KEY）。
"""

import json

from loguru import logger

from app.core.redis import get_redis

KEY = "user_llm:{user_id}"
TTL_SECONDS = 30 * 24 * 3600  # 30 天


async def get_user_llm_config(user_id: str) -> dict | None:
    try:
        r = get_redis()
        raw = await r.get(KEY.format(user_id=user_id))
        if not raw:
            return None
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取用户 LLM 配置失败: {}", exc)
        return None


async def set_user_llm_config(user_id: str, cfg: dict) -> None:
    r = get_redis()
    await r.set(KEY.format(user_id=user_id), json.dumps(cfg, ensure_ascii=False), ex=TTL_SECONDS)


async def clear_user_llm_config(user_id: str) -> None:
    r = get_redis()
    await r.delete(KEY.format(user_id=user_id))
