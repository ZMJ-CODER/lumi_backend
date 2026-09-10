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


def _limit(_channel: str) -> int:
    """Use the DAG concurrency ceiling for non-provider execution leases."""
    return max(1, int(settings.AGENT_NODE_CONCURRENCY or 1))


def _llm_limit() -> int:
    """LLM 专用并发预算，不随普通 DAG 并发上限线性放大。"""
    return max(1, int(getattr(settings, "AGENT_LLM_MAX_CONCURRENCY", 5) or 5))


class ChannelLimiter(KernelChannelLimiter):
    """Binds generic lease coordination to Lumi's Redis and metrics."""

    async def _redis(self) -> Any | None:
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001
            return None

    def _limit(self, channel: str) -> int:
        if channel == "llm_provider":
            return _llm_limit()
        return _limit(channel)

    def _observe_wait(self, channel: str, seconds: float) -> None:
        try:
            from app.core.observability import observe_agent_channel_wait

            observe_agent_channel_wait(channel, seconds)
        except Exception:  # noqa: BLE001
            pass


channel_limiter = ChannelLimiter()
