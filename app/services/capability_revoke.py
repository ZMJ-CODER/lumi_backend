"""跨 worker 撤销广播（Redis pub/sub）与本地缓存失效。

**为什么需要**：租约的权威副本在 Redis，但每个 worker 都持有一份**读缓存**供 Broker
同步选路。撤销（客户端主动上报/健康失败/管理端强制）如果只改 Redis，别的 worker 的
缓存仍会在窗口期内继续派发——"撤销了但还被调用"是最难排查的一类问题。

设计取舍：

* **pub/sub 而不是 stream**：撤销只是"失效本地缓存"的信号，丢一条不会造成错误决策
  （下次 ``refresh_from_redis`` 一样会拿到最新租约）；不需要可靠消费。
* 收到信号**只清缓存**，真正的权威数据仍在 Redis —— 这样"撤销广播丢失"最坏是
  多一次 Redis 往返，而不是把已撤销的租约当成活的。
* Redis 不可用时全部降级为 no-op（单 worker 本来就只有一个缓存）。

事件形状（与方案里设计的 ``capability_revoked`` 对齐）：:

    {"type": "capability_revoked", "provider_id": ..., "plugin_id": ...,
     "lease_id": ..., "capability": ..., "reason": ..., "occurred_at": ...}
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

#: 撤销广播频道（所有 API worker 都订阅它）。
REVOKE_CHANNEL = "broker:events:revoked"
#: 事件类型名（与前端/审计词表一致）。
EVENT_CAPABILITY_REVOKED = "capability_revoked"

#: 允许的撤销原因（稳定词表；未知值收敛为 other）。
REVOKE_REASONS: tuple[str, ...] = (
    "provider_unhealthy",
    "provider_unregistered",
    "provider_disabled",
    "lease_expired",
    "plugin_uninstalled",
    "admin_revoked",
    "other",
)


def normalize_revoke_reason(value: Any) -> str:
    key = str(value or "").strip().casefold()
    return key if key in REVOKE_REASONS else "other"


@dataclass(slots=True)
class RevokeEvent:
    """一条撤销事件（可 JSON 序列化，不含参数/正文）。"""

    capability: str = ""
    provider_id: str = ""
    plugin_id: str = ""
    lease_id: str = ""
    reason: str = "other"
    occurred_at: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": EVENT_CAPABILITY_REVOKED,
            "capability": self.capability,
            "provider_id": self.provider_id,
            "plugin_id": self.plugin_id,
            "lease_id": self.lease_id,
            "reason": normalize_revoke_reason(self.reason),
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RevokeEvent":
        return cls(
            capability=str(payload.get("capability") or ""),
            provider_id=str(payload.get("provider_id") or ""),
            plugin_id=str(payload.get("plugin_id") or ""),
            lease_id=str(payload.get("lease_id") or ""),
            reason=normalize_revoke_reason(payload.get("reason")),
            occurred_at=float(payload.get("occurred_at") or time.time()),
        )


class RevokeBus:
    """撤销事件的发布/订阅（Redis pub/sub；不可用时 no-op）。"""

    def __init__(self, *, channel: str = REVOKE_CHANNEL) -> None:
        self._channel = str(channel)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._handlers: list[Any] = []
        self._invalidations = 0

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def invalidations(self) -> int:
        """本进程收到并处理的失效次数（观测用）。"""
        return self._invalidations

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @staticmethod
    def _redis() -> Any | None:
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001
            return None

    async def publish(self, event: RevokeEvent) -> bool:
        """广播一条撤销（失败只记日志：调用方已直接改过 Redis 权威副本）。"""
        redis = self._redis()
        if redis is None:
            return False
        try:
            await redis.publish(self._channel, json.dumps(event.to_payload(), ensure_ascii=False))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[capability] 撤销广播失败（忽略）: {}", str(exc)[:120])
            return False

    def on_revoke(self, handler: Any) -> None:
        """注册一个失效回调（同步或异步均可）。"""
        self._handlers.append(handler)

    async def _dispatch(self, event: RevokeEvent) -> None:
        self._invalidations += 1
        logger.info(
            "[capability] 收到撤销广播 capability={} provider={} reason={}（本地缓存已失效）",
            event.capability or "-", event.provider_id or "-", event.reason,
        )
        for handler in list(self._handlers):
            try:
                result = handler(event)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:  # noqa: BLE001 - 单个回调失败不影响其它
                logger.debug("[capability] 撤销回调失败: {}", str(exc)[:120])

    async def start(self, *, on_revoke: Any = None) -> bool:
        """启动订阅（幂等）。返回是否真的订阅上了 Redis。"""
        if on_revoke is not None:
            self.on_revoke(on_revoke)
        if self.running:
            return True
        redis = self._redis()
        if redis is None:
            return False
        self._stop = asyncio.Event()

        async def _listen() -> None:
            pubsub = None
            try:
                pubsub = redis.pubsub()
                await pubsub.subscribe(self._channel)
                while not self._stop.is_set():
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if not message:
                        await asyncio.sleep(0.05)
                        continue
                    raw = message.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "ignore")
                    try:
                        payload = json.loads(raw or "{}")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict) and payload.get("type") == EVENT_CAPABILITY_REVOKED:
                        await self._dispatch(RevokeEvent.from_payload(payload))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 订阅循环不因单次错误退出
                logger.warning("[capability] 撤销订阅中断: {}", str(exc)[:160])
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe(self._channel)
                        await pubsub.close()
                    except Exception:  # noqa: BLE001
                        pass

        self._task = asyncio.create_task(_listen())
        return True

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


#: 进程内共享总线。
revoke_bus = RevokeBus()


def _invalidate_local_cache(event: RevokeEvent) -> None:
    """收到撤销 → 清空租约读缓存（权威数据仍在 Redis，下次 refresh 会重建）。"""
    from app.agents.capabilities.registry.registry import capability_registry
    from app.services.capability_lease import capability_lease_service

    try:
        capability_lease_service.invalidate_redis_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[capability] 清空租约缓存失败: {}", str(exc)[:120])
    if event.provider_id:
        try:
            capability_registry.unregister(event.provider_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[capability] 摘除注册表条目失败: {}", str(exc)[:120])


async def start_revoke_listener() -> bool:
    """启动时调用：订阅撤销广播并接上"清本地缓存 + 摘注册表"。"""
    return await revoke_bus.start(on_revoke=_invalidate_local_cache)


async def stop_revoke_listener() -> None:
    await revoke_bus.stop()


async def broadcast_revoke(
    *,
    capability: str = "",
    provider_id: str = "",
    plugin_id: str = "",
    lease_id: str = "",
    reason: str = "other",
) -> bool:
    """发布一条撤销广播（撤销方在改完 Redis 权威副本后调用）。"""
    return await revoke_bus.publish(
        RevokeEvent(
            capability=capability,
            provider_id=provider_id,
            plugin_id=plugin_id,
            lease_id=lease_id,
            reason=reason,
        )
    )


__all__ = [
    "EVENT_CAPABILITY_REVOKED",
    "REVOKE_CHANNEL",
    "REVOKE_REASONS",
    "RevokeBus",
    "RevokeEvent",
    "broadcast_revoke",
    "normalize_revoke_reason",
    "revoke_bus",
    "start_revoke_listener",
    "stop_revoke_listener",
]
