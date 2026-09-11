"""跨 worker 租约注册表（Redis 权威 + 进程内读缓存）。

**为什么需要**：进程内租约在多 API worker 下必然出错——worker A 收到注册、worker B
执行能力却查不到租约（表现为随机 ``CAPABILITY_MISSING``）。因此租约的**权威副本在
Redis**，每个 worker 只保留一份**读缓存**用于 Broker 的同步选路。

Redis 里拆三类数据（与运维/排障习惯一致）：

* ``broker:lease:{lease_id}``      —— 一条租约（Hash，TTL 与 ``expires_at`` 对齐）
* ``broker:capability:{workspace_id}:{capability}`` —— 能力索引（Set，存 lease_id）
* ``broker:index:all``             —— 全局索引（Sorted set，score=expires_at，
  ``workspace_id`` 为空的能力用它来查）
* ``broker:provider:{provider_id}:health`` —— Provider 健康（Hash，与租约**分开**：
  健康变化不等于租约结构变化）

读路径：Broker 用 :meth:`RedisLeaseRegistry.snapshot_cached` 同步取本地缓存；
:meth:`RedisLeaseRegistry.refresh` 从 Redis 拉取权威数据。Redis 不可用时**自动降级**
为纯进程内（单 worker 仍可用），并记录一次告警——降级不等于无界重试。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from lumi_contracts.plugins import ProviderHealth, ProviderLease
from lumi_contracts.plugins.provider_lease import lease_id_hint

#: Redis key 前缀与 schema 版本（结构变更时升版本，避免旧数据被误读）。
LEASE_KEY_PREFIX = "broker:lease:"
CAPABILITY_KEY_PREFIX = "broker:capability:"
PROVIDER_HEALTH_KEY_PREFIX = "broker:provider:"
GLOBAL_INDEX_KEY = "broker:index:all"
SCHEMA_VERSION = 1

#: 读缓存最长陈旧时间（秒）。超过则 refresh 失败时不再信任本地缓存。
CACHE_MAX_AGE_SECONDS = 30.0


def lease_id_for(provider_id: str, capability: str) -> str:
    """租约主键（转发到契约层 ``lease_id_hint``：客户端与服务端必须算出同一个）。"""
    return lease_id_hint(provider_id, capability)


def capability_index_key(capability: str, workspace_id: str = "") -> str:
    """能力索引 key：有工作区就按工作区隔离（精确），否则落全局 Sorted set。"""
    base = str(capability or "").split("@", 1)[0]
    workspace = str(workspace_id or "").strip()
    if workspace:
        return f"{CAPABILITY_KEY_PREFIX}{workspace}:{base}"
    return GLOBAL_INDEX_KEY


def health_key(provider_id: str) -> str:
    return f"{PROVIDER_HEALTH_KEY_PREFIX}{str(provider_id or '')}:health"


def _now() -> float:
    return time.time()


class RedisLeaseRegistry:
    """租约的 Redis 权威副本 + 进程内读缓存。"""

    def __init__(self, *, cache_max_age: float = CACHE_MAX_AGE_SECONDS) -> None:
        self._cache: dict[str, ProviderLease] = {}
        self._cache_at: float = 0.0
        self._cache_max_age = max(0.0, float(cache_max_age))
        self._redis_available: bool | None = None

    # ── Redis 句柄（不可用时返回 None，绝不抛给调用方）────────────

    @staticmethod
    def _redis() -> Any | None:
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001 - 未初始化/不可用都按降级处理
            return None

    # ── 写（注册/心跳/注销）─────────────────────────────────────

    async def publish_leases(
        self,
        leases: list[ProviderLease],
        *,
        health: dict[str, Any] | None = None,
    ) -> int:
        """把租约写入 Redis（TTL 与 ``expires_at`` 对齐）。

        同时维护能力索引与 Provider 健康；返回成功写入的租约条数（Redis 不可用时 0，
        调用方据此知道"只有本地副本"）。
        """
        redis = self._redis()
        if redis is None or not leases:
            return 0
        written = 0
        for lease in leases:
            lease_id = lease_id_for(lease.provider_id, lease.capability)
            ttl = max(1, int(float(lease.expires_at or 0) - _now()))
            key = f"{LEASE_KEY_PREFIX}{lease_id}"
            payload = {
                "schema_version": SCHEMA_VERSION,
                "lease_id": lease_id,
                "provider_id": lease.provider_id,
                "capability": lease.capability,
                "contract_version": int(lease.contract_version or 1),
                "plugin_id": lease.plugin_id,
                "plugin_version": lease.plugin_version,
                "provider_version": lease.provider_version,
                "user_id": lease.user_id,
                "device_id": lease.device_id,
                "conversation_id": lease.conversation_id,
                "workspace_id": lease.workspace_id,
                "session_id": lease.session_id,
                "deployment": str(lease.deployment),
                "trust_level": str(lease.trust_level),
                "health_status": str(lease.health_status),
                "scope": json.dumps(dict(lease.scope or {}), ensure_ascii=False),
                "issued_at": float(lease.issued_at or 0.0),
                "expires_at": float(lease.expires_at or 0.0),
                "last_heartbeat_at": float(lease.last_heartbeat_at or 0.0),
                "status": "active",
            }
            try:
                await redis.hset(key, mapping=payload)
                # TTL 与 expires_at 对齐：过期由 Redis 自己清理，避免"残影路由"。
                await redis.expire(key, ttl)
                index_key = capability_index_key(lease.capability, lease.workspace_id)
                if index_key == GLOBAL_INDEX_KEY:
                    await redis.zadd(index_key, {lease_id: float(lease.expires_at or 0.0)})
                else:
                    await redis.sadd(index_key, lease_id)
                    await redis.expire(index_key, ttl)
                written += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它
                logger.warning("[capability] 租约写入 Redis 失败（降级为本地）: {}", str(exc)[:120])
        if health:
            await self.publish_health(health)
        return written

    async def publish_health(self, health: dict[str, Any]) -> bool:
        """Provider 健康单独一份（健康变化 ≠ 租约结构变化）。"""
        redis = self._redis()
        provider_id = str(health.get("provider_id") or "")
        if redis is None or not provider_id:
            return False
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": str(health.get("status") or ProviderHealth.UNKNOWN.value),
            "last_seen_at": float(health.get("last_seen_at") or _now()),
            "worker_id": str(health.get("worker_id") or ""),
            "latency_ms": int(health.get("latency_ms") or 0),
        }
        try:
            key = health_key(provider_id)
            await redis.hset(key, mapping=payload)
            # 健康记录比租约活得久一点（便于排查"刚掉线时发生了什么"）。
            await redis.expire(key, 300)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[capability] 健康写入 Redis 失败: {}", str(exc)[:120])
            return False

    async def remove_leases(self, leases: list[ProviderLease]) -> int:
        """注销/撤销：删租约 + 从能力索引移除（恢复必须重新注册）。"""
        redis = self._redis()
        if redis is None or not leases:
            return 0
        removed = 0
        for lease in leases:
            lease_id = lease_id_for(lease.provider_id, lease.capability)
            try:
                await redis.delete(f"{LEASE_KEY_PREFIX}{lease_id}")
                index_key = capability_index_key(lease.capability, lease.workspace_id)
                if index_key == GLOBAL_INDEX_KEY:
                    await redis.zrem(index_key, lease_id)
                else:
                    await redis.srem(index_key, lease_id)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[capability] 删除 Redis 租约失败: {}", str(exc)[:120])
        return removed

    # ── 读（refresh + 本地缓存）────────────────────────────────

    async def refresh(self) -> list[ProviderLease]:
        """从 Redis 拉取全部活租约并刷新本地缓存（权威 → 缓存）。"""
        redis = self._redis()
        if redis is None:
            self._redis_available = False
            return self.snapshot_cached()
        try:
            leases = await self._load_all(redis)
        except Exception as exc:  # noqa: BLE001 - 读取失败保留旧缓存
            self._redis_available = False
            logger.warning("[capability] 租约读取 Redis 失败（沿用本地缓存）: {}", str(exc)[:120])
            return self.snapshot_cached()
        self._redis_available = True
        self._cache = {
            lease_id_for(lease.provider_id, lease.capability): lease for lease in leases
        }
        self._cache_at = _now()
        return list(self._cache.values())

    async def _load_all(self, redis: Any) -> list[ProviderLease]:
        now = _now()
        # 全局索引：先清掉已过期的 score 成员，再取活租约。
        await redis.zremrangebyscore(GLOBAL_INDEX_KEY, "-inf", now)
        lease_ids: set[str] = set(await redis.zrangebyscore(GLOBAL_INDEX_KEY, now, "+inf"))
        # 工作区索引是散落的 Set，按前缀扫描后并入。
        async for raw_key in redis.scan_iter(match=f"{CAPABILITY_KEY_PREFIX}*"):
            lease_ids.update(await redis.smembers(raw_key))
        leases: list[ProviderLease] = []
        for lease_id in lease_ids:
            payload = await redis.hgetall(f"{LEASE_KEY_PREFIX}{lease_id}")
            if not payload:
                continue
            lease = self._decode_lease(payload)
            if lease is not None and not lease.is_expired(now=now):
                leases.append(lease)
        return leases

    @staticmethod
    def _decode_lease(payload: dict[str, Any]) -> ProviderLease | None:
        try:
            scope = payload.get("scope") or "{}"
            return ProviderLease(
                provider_id=str(payload.get("provider_id") or ""),
                capability=str(payload.get("capability") or ""),
                contract_version=int(payload.get("contract_version") or 1),
                user_id=str(payload.get("user_id") or ""),
                device_id=str(payload.get("device_id") or ""),
                conversation_id=str(payload.get("conversation_id") or ""),
                workspace_id=str(payload.get("workspace_id") or ""),
                session_id=str(payload.get("session_id") or ""),
                deployment=str(payload.get("deployment") or "client"),
                trust_level=str(payload.get("trust_level") or "third_party"),
                plugin_id=str(payload.get("plugin_id") or ""),
                plugin_version=str(payload.get("plugin_version") or ""),
                provider_version=str(payload.get("provider_version") or ""),
                scope=json.loads(scope) if isinstance(scope, str) else dict(scope or {}),
                health_status=str(payload.get("health_status") or "unknown"),
                issued_at=float(payload.get("issued_at") or 0.0),
                expires_at=float(payload.get("expires_at") or 0.0),
                last_heartbeat_at=float(payload.get("last_heartbeat_at") or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 - 坏数据只跳过这一条
            logger.warning("[capability] 租约解码失败（已跳过）: {}", str(exc)[:120])
            return None

    def snapshot_cached(self) -> list[ProviderLease]:
        """Broker 的同步读路径：返回本地缓存（过期条目顺带剔除）。"""
        now = _now()
        fresh = {
            key: lease for key, lease in self._cache.items() if not lease.is_expired(now=now)
        }
        self._cache = fresh
        return list(fresh.values())

    @property
    def cache_age_seconds(self) -> float:
        return max(0.0, _now() - self._cache_at) if self._cache_at else float("inf")

    @property
    def cache_is_fresh(self) -> bool:
        return self.cache_age_seconds <= self._cache_max_age

    @property
    def redis_available(self) -> bool | None:
        return self._redis_available

    def seed_cache(self, leases: list[ProviderLease]) -> None:
        """把租约直接放进本地缓存（注册/心跳就在本 worker 时省一次 Redis 往返）。"""
        for lease in leases:
            self._cache[lease_id_for(lease.provider_id, lease.capability)] = lease
        self._cache_at = _now()

    def clear_cache(self) -> None:
        """清空本地缓存（收到撤销广播时用；权威数据仍在 Redis，下次 refresh 重建）。"""
        self._cache.clear()
        self._cache_at = 0.0

    def health(self, provider_id: str) -> dict[str, Any]:
        """读本地已知的 Provider 健康（不查 Redis；Broker 选路只用它做粗筛）。"""
        rows = [
            lease
            for lease in self.snapshot_cached()
            if lease.provider_id == str(provider_id or "")
        ]
        if not rows:
            return {"provider_id": str(provider_id or ""), "status": ProviderHealth.UNKNOWN.value}
        return {
            "provider_id": str(provider_id),
            "status": str(rows[0].health_status),
            "lease_count": len(rows),
        }


__all__ = [
    "CACHE_MAX_AGE_SECONDS",
    "CAPABILITY_KEY_PREFIX",
    "GLOBAL_INDEX_KEY",
    "LEASE_KEY_PREFIX",
    "PROVIDER_HEALTH_KEY_PREFIX",
    "RedisLeaseRegistry",
    "SCHEMA_VERSION",
    "capability_index_key",
    "health_key",
    "lease_id_for",
]
