"""外部依赖韧性：分布式熔断器，Redis 不可用时安全回退进程内状态。

熔断器只保护服务端暂时性故障。调用方传入的密钥、模型名、余额等配置问题
不应计入失败，否则一个错误配置会影响同一供应商的其他用户。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from app.core.config import settings

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """依赖正处于保护窗口，调用方应走已有降级或快速失败。"""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = max(1, int(retry_after + 0.999))
        super().__init__(f"外部服务 {name} 暂时保护中，请在 {self.retry_after} 秒后重试")


def is_transient_dependency_error(exc: Exception) -> bool:
    """判断是否为供应商瞬时故障，严格排除用户配置类 4xx。"""
    if isinstance(exc, CircuitOpenError):
        return True
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException, TimeoutError, ConnectionError)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    text = str(exc).lower()
    # LangChain/SDK 常将 HTTP 状态包装为普通异常；只匹配明确的临时码。
    return any(token in text for token in ("timeout", "timed out", "connection", "rate limit", "too many requests", "status code: 429", "status code: 5", "http 5"))


@dataclass
class _LocalCircuitState:
    failures: int = 0
    opened_until: float = 0.0
    probe_until: float = 0.0


_local_states: dict[str, _LocalCircuitState] = {}
_local_lock = asyncio.Lock()


class AsyncCircuitBreaker:
    """Redis 优先的 closed/open/half-open 熔断器。"""

    _BEFORE_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {1, 0} end
local ok, state = pcall(cjson.decode, raw)
if not ok then redis.call('DEL', KEYS[1]); return {1, 0} end
local now = tonumber(ARGV[1])
if (state.opened_until or 0) > now then return {0, state.opened_until - now} end
if (state.probe_until or 0) > now then return {0, state.probe_until - now} end
if (state.opened_until or 0) > 0 then
  state.probe_until = now + tonumber(ARGV[2])
  redis.call('SET', KEYS[1], cjson.encode(state), 'EX', tonumber(ARGV[3]))
end
return {1, 0}
"""
    _FAIL_LUA = """
local raw = redis.call('GET', KEYS[1])
local state = {failures=0, opened_until=0, probe_until=0}
if raw then
  local ok, decoded = pcall(cjson.decode, raw)
  if ok then state = decoded end
end
local now = tonumber(ARGV[1])
state.failures = (state.failures or 0) + 1
state.probe_until = 0
if state.failures >= tonumber(ARGV[2]) then
  state.opened_until = now + tonumber(ARGV[3])
end
redis.call('SET', KEYS[1], cjson.encode(state), 'EX', tonumber(ARGV[4]))
return state.failures
"""

    def __init__(self, name: str, *, failure_threshold: int | None = None, recovery_seconds: float | None = None) -> None:
        self.name = name
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
        self._key = f"resilience:circuit:{digest}"
        self.failure_threshold = max(1, failure_threshold or settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD)
        self.recovery_seconds = float(recovery_seconds or settings.CIRCUIT_BREAKER_RECOVERY_SECONDS)

    async def before_call(self) -> None:
        now = time.time()
        try:
            from app.core.redis import get_redis

            result = await get_redis().eval(
                self._BEFORE_LUA,
                1,
                self._key,
                now,
                settings.CIRCUIT_BREAKER_HALF_OPEN_PROBE_SECONDS,
                max(1, int(self.recovery_seconds * 2)),
            )
            if not int(result[0]):
                raise CircuitOpenError(self.name, float(result[1]))
            return
        except CircuitOpenError:
            raise
        except Exception:
            # Redis 故障不能使业务整体不可用；同进程仍可避免热循环打爆依赖。
            async with _local_lock:
                state = _local_states.setdefault(self._key, _LocalCircuitState())
                if state.opened_until > now:
                    raise CircuitOpenError(self.name, state.opened_until - now)
                if state.probe_until > now:
                    raise CircuitOpenError(self.name, state.probe_until - now)
                if state.opened_until:
                    state.probe_until = now + settings.CIRCUIT_BREAKER_HALF_OPEN_PROBE_SECONDS

    async def record_success(self) -> None:
        try:
            from app.core.redis import get_redis

            await get_redis().delete(self._key)
        except Exception:
            async with _local_lock:
                _local_states.pop(self._key, None)

    async def record_failure(self, exc: Exception) -> None:
        if not is_transient_dependency_error(exc):
            return
        now = time.time()
        try:
            from app.core.redis import get_redis

            await get_redis().eval(
                self._FAIL_LUA,
                1,
                self._key,
                now,
                self.failure_threshold,
                self.recovery_seconds,
                max(1, int(self.recovery_seconds * 2)),
            )
        except Exception:
            async with _local_lock:
                state = _local_states.setdefault(self._key, _LocalCircuitState())
                state.failures += 1
                state.probe_until = 0.0
                if state.failures >= self.failure_threshold:
                    state.opened_until = now + self.recovery_seconds

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        await self.before_call()
        try:
            result = await operation()
        except Exception as exc:
            await self.record_failure(exc)
            raise
        await self.record_success()
        return result


_breakers: dict[str, AsyncCircuitBreaker] = {}


def get_breaker(name: str) -> AsyncCircuitBreaker:
    """获取进程内门面；实际状态由 Redis 在 worker 间共享。"""
    return _breakers.setdefault(name, AsyncCircuitBreaker(name))
