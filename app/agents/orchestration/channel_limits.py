"""内核通道并发租约的 Lumi 配置与指标适配器。"""

from __future__ import annotations

import asyncio  # noqa: F401 - Backward-compatible test seam for lease timing.
from typing import Any

from lumi_orch.runner import (
    CHANNEL_ACQUIRE_SCRIPT,
    CHANNEL_RENEW_SCRIPT,
    ChannelLimiter as KernelChannelLimiter,
)

from app.core.config import settings


# Compatibility aliases for existing operational tests and integrations.  The
# scripts themselves remain owned by the backend-neutral kernel runner.
_ACQUIRE = CHANNEL_ACQUIRE_SCRIPT
_RENEW = CHANNEL_RENEW_SCRIPT


def _limit(channel: str) -> int:
    return max(1, {
        "direct_llm": settings.AGENT_CHANNEL_DIRECT_LLM_CONCURRENCY,
        "deterministic_script": settings.AGENT_CHANNEL_SCRIPT_CONCURRENCY,
        "rag": settings.AGENT_CHANNEL_RAG_CONCURRENCY,
        "agent": settings.AGENT_CHANNEL_AGENT_CONCURRENCY,
    }.get(channel, settings.AGENT_CHANNEL_AGENT_CONCURRENCY))


class ChannelLimiter(KernelChannelLimiter):
    """Binds generic lease coordination to Lumi's Redis and metrics."""

    async def _redis(self) -> Any | None:
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001
            return None

    def _limit(self, channel: str) -> int:
        return _limit(channel)

    def _observe_wait(self, channel: str, seconds: float) -> None:
        try:
            from app.core.observability import observe_agent_channel_wait

            observe_agent_channel_wait(channel, seconds)
        except Exception:  # noqa: BLE001
            pass


channel_limiter = ChannelLimiter()
