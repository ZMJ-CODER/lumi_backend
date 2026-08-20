"""外部依赖熔断与令牌桶的核心行为测试（无 Redis 时验证本地安全降级）。"""

import asyncio

import httpx
import pytest

from app.core import resilience, throttling
from app.core.resilience import AsyncCircuitBreaker, CircuitOpenError
from app.core.throttling import TokenBucketLimiter


def test_circuit_opens_after_transient_failures_and_recovers():
    async def scenario():
        resilience._local_states.clear()
        breaker = AsyncCircuitBreaker("test:dependency", failure_threshold=2, recovery_seconds=0.02)

        async def down():
            raise httpx.ConnectError("down")

        with pytest.raises(httpx.ConnectError):
            await breaker.call(down)
        with pytest.raises(httpx.ConnectError):
            await breaker.call(down)
        with pytest.raises(CircuitOpenError):
            await breaker.before_call()
        await asyncio.sleep(0.03)
        await breaker.call(lambda: asyncio.sleep(0, result="ok"))
        await breaker.before_call()  # 成功的半开探测关闭熔断器

    asyncio.run(scenario())


def test_circuit_does_not_open_for_user_configuration_4xx():
    async def scenario():
        resilience._local_states.clear()
        breaker = AsyncCircuitBreaker("test:bad-request", failure_threshold=1, recovery_seconds=30)
        response = httpx.Response(401, request=httpx.Request("POST", "https://example.test"))
        error = httpx.HTTPStatusError("unauthorized", request=response.request, response=response)

        async def invalid():
            raise error

        with pytest.raises(httpx.HTTPStatusError):
            await breaker.call(invalid)
        await breaker.before_call()

    asyncio.run(scenario())


def test_token_bucket_isolated_by_user_and_returns_retry_after():
    async def scenario():
        throttling._local_buckets.clear()
        limiter = TokenBucketLimiter("test", capacity=2, refill_per_minute=60)
        assert (await limiter.consume("user-a")).allowed
        assert (await limiter.consume("user-a")).allowed
        denied = await limiter.consume("user-a")
        assert not denied.allowed
        assert denied.retry_after >= 1
        assert (await limiter.consume("user-b")).allowed

    asyncio.run(scenario())
