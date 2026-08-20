"""LLM 动态配置管理 —— Redis 存储，进程内短缓存，.env 兜底.

设计原则:
  - 配置只包含 "base_url + api_key + model + timeout" 三元组，不绑定具体 provider，
    将来切换模型网关时只需把这三个值换成网关的地址/key/别名即可。
  - 读取优先级: 场景级 Redis 配置 → 全局 Redis 配置 → .env 兜底。
  - 进程内缓存 TTL 默认 5 秒，配置更新后最多 5 秒全局生效，无需重启进程。
  - Redis 不可用时静默回落 .env，保证后端不因配置源故障而崩溃。
"""

import json
import time
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.redis import get_redis

# Redis Key 模板
LLM_CONFIG_KEY = "config:llm:default"
LLM_CONFIG_SCENE_KEY = "config:llm:scene:{scene}"

# 进程内缓存: {key: (expire_at, cfg_or_None)}
_cache: dict[str, tuple[float, dict | None]] = {}

# 配置生效延迟（秒）
CACHE_TTL = 5.0


def _env_fallback(scene: str | None = None, provider: str | None = None) -> dict:
    """.env 兜底配置（保留 LLM_PROVIDER 选择逻辑；普通聊天场景默认多模态模型）."""
    provider = provider or settings.LLM_PROVIDER
    # 普通模式（chat）：Qwen-VL-Plus 多模态，131k 上下文
    if scene == "chat":
        return {
            "base_url": settings.QWEN_BASE_URL,
            "api_key": settings.QWEN_API_KEY,
            "model": settings.QWEN_VL_MODEL,
            "timeout": 120,
            "source": "env",
        }
    if provider == "qwen":
        return {
            "base_url": settings.QWEN_BASE_URL,
            "api_key": settings.QWEN_API_KEY,
            "model": settings.QWEN_MODEL,
            "timeout": 120,
            "source": "env",
        }
    return {
        "base_url": settings.DEEPSEEK_BASE_URL,
        "api_key": settings.DEEPSEEK_API_KEY,
        "model": settings.DEEPSEEK_MODEL,
        "timeout": 120,
        "source": "env",
    }


async def _read_from_redis(key: str) -> dict | None:
    """带进程内缓存的 Redis 读取；任何异常都回落为 None."""
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    cfg: dict | None = None
    try:
        r = get_redis()
        raw = await r.get(key)
        if raw:
            cfg = json.loads(raw)
    except Exception as e:  # noqa: BLE001 - 配置源故障不允许影响主流程
        logger.warning("读取 LLM 动态配置失败，回落 .env: {}", e)
    _cache[key] = (now + CACHE_TTL, cfg)
    return cfg


def _invalidate_cache(key: str) -> None:
    _cache.pop(key, None)


async def _resolve_user_cfg(user_cfg: dict, provider: str | None) -> dict | None:
    """用户级配置 → 可执行的 {base_url, api_key, model, ...}.

    非 BYOK：api_key 用服务端 .env 里对应 provider 的密钥；
    BYOK：api_key 留空，由 LLMClient 用请求头临时携带的 key 覆盖（绝不落库）。
    """
    from app.core.model_catalog import PROVIDER_BASE_URLS

    prov = user_cfg.get("provider") or provider
    base_url = ""
    if user_cfg.get("byok") and user_cfg.get("base_url"):
        from app.core.model_catalog import normalize_byok_base_url

        try:
            base_url = normalize_byok_base_url(
                str(user_cfg["base_url"]), allow_private=settings.BYOK_ALLOW_PRIVATE_BASE_URL
            )
        except ValueError:
            logger.warning("忽略无效的用户 BYOK endpoint")
            return None
    else:
        base_url = PROVIDER_BASE_URLS.get(prov or "") or ""
    if not base_url:
        return None
    env_key = ""
    if prov == "qwen":
        env_key = settings.QWEN_API_KEY
    elif prov == "deepseek":
        env_key = settings.DEEPSEEK_API_KEY
    cfg = {
        "base_url": base_url,
        "api_key": "" if user_cfg.get("byok") else env_key,
        "model": user_cfg.get("model") or "",
        "timeout": 120,
        "source": "user",
        "byok": bool(user_cfg.get("byok")),
    }
    if user_cfg.get("reasoning_effort"):
        cfg["reasoning_effort"] = user_cfg["reasoning_effort"]
    return cfg


async def get_llm_config(
    scene: str | None = None,
    provider: str | None = None,
    user_id: str | None = None,
) -> dict:
    """获取生效的 LLM 配置: 用户级 → 场景级 → 全局默认 → .env 兜底."""
    if user_id:
        from app.services.user_llm_config import get_user_llm_config

        user_cfg = await get_user_llm_config(user_id)
        if user_cfg:
            resolved = await _resolve_user_cfg(user_cfg, provider)
            if resolved:
                return resolved
    if scene:
        cfg = await _read_from_redis(LLM_CONFIG_SCENE_KEY.format(scene=scene))
        if cfg:
            return cfg
    cfg = await _read_from_redis(LLM_CONFIG_KEY)
    if cfg:
        return cfg
    return _env_fallback(scene, provider)


async def set_llm_config(cfg: dict[str, Any], scene: str | None = None) -> None:
    """写入 LLM 配置到 Redis，并让进程内缓存立即失效."""
    key = LLM_CONFIG_SCENE_KEY.format(scene=scene) if scene else LLM_CONFIG_KEY
    r = get_redis()
    await r.set(key, json.dumps(cfg, ensure_ascii=False))
    _invalidate_cache(key)


async def reset_llm_config(scene: str | None = None) -> None:
    """删除 Redis 中的 LLM 配置，回落 .env 默认值."""
    key = LLM_CONFIG_SCENE_KEY.format(scene=scene) if scene else LLM_CONFIG_KEY
    try:
        r = get_redis()
        await r.delete(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("重置 LLM 动态配置失败（忽略）: {}", e)
    finally:
        _invalidate_cache(key)


async def validate_llm_config(cfg: dict[str, Any]) -> tuple[bool, str]:
    """用候选配置发一条最小请求，验证连通性后再允许写入."""
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    timeout = float(cfg.get("timeout") or 120)

    if not base_url or not api_key or not model:
        return False, "base_url / api_key / model 均不能为空"

    try:
        # 延迟导入，避免 ``langchain.models -> llm_config`` 的初始化环依赖。
        from langchain_core.messages import HumanMessage

        from app.agents.langchain.models import get_chat_model

        chat_model = await get_chat_model(
            scene=None,
            user_id=None,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=min(timeout, 15.0),
            max_tokens=1,
        )
        await chat_model.ainvoke([HumanMessage(content="ping")])
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
