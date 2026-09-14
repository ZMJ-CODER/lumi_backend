"""写操作代际校验：读路径 Fail-Open，**写路径 Fail-Closed**。

## 为什么读写要分开

策略/租约这类"本地缓存 + Redis 权威"的结构，读路径必须 Fail-Open：Redis 抖一下不该
让整个服务不可用。但**写操作**一旦基于过期或被撤销的授权执行，造成的是不可回滚的
副作用（改文件、发消息、提交数据）。所以写侧必须反过来：

* 本地**没有**缓存 → 阻断（"没缓存"绝不等于"默认允许"）；
* 本地缓存**超过 lease_ttl** → 阻断；
* Redis 可达但**代际（generation）不一致** → 阻断（说明有新状态没同步到本进程，
  例如管理员刚撤销/改绑）；
* Redis **报错/不可达** → 阻断；
* 只有"缓存新鲜 **且** 代际一致"才放行。

这保证了"Redis 挂了时，只要缓存新鲜且代际没变（说明期间没发生过状态变更）可以继续写；
一旦有任何异常迹象，直接 Fail-Closed"。

## 缓存结构

每条记录两个时间字段（方案原样）：

* ``lease_granted_at``：本地拿到/确认这份授权的时刻（unix 秒）；
* ``lease_ttl``：这份授权最长可用多久（秒）。

外加 ``generation``：Redis 侧每次状态变更都会 ``INCR`` 代际键，因此"代际不一致"
是"期间有人改过状态"的充分信号——不需要比对内容，也不需要额外的事件通道。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

#: 代际键前缀：``write_gate:generation:<scope>``。
GENERATION_KEY_PREFIX = "write_gate:generation:"


class WriteGateDenied(RuntimeError):
    """写操作被写闸拒绝（附带稳定原因码，便于前端/审计区分）。"""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = str(reason or "")
        self.message = str(message or "")


def write_gate_enabled() -> bool:
    """``WRITE_GATE_ENFORCEMENT``（默认关闭 = 完全不影响既有写路径）。"""
    try:
        from app.platform.runtime.feature_flags import feature_enabled

        return feature_enabled("WRITE_GATE_ENFORCEMENT")
    except Exception:  # noqa: BLE001
        return False


def lease_ttl_seconds() -> int:
    try:
        from app.core.config import settings

        return max(1, int(getattr(settings, "WRITE_GATE_LEASE_TTL_SECONDS", 120) or 120))
    except Exception:  # noqa: BLE001
        return 120


def generation_ttl_seconds() -> int:
    try:
        from app.core.config import settings

        return max(1, int(getattr(settings, "WRITE_GATE_REDIS_TTL_SECONDS", 600) or 600))
    except Exception:  # noqa: BLE001
        return 600


def generation_key(scope: str = "default") -> str:
    return f"{GENERATION_KEY_PREFIX}{str(scope or 'default')}"


#: 拒绝原因码（稳定契约：前端/审计按码分派，不解析文案）。
REASON_NO_CACHE = "WRITE_GATE_NO_LEASE"
REASON_LEASE_EXPIRED = "WRITE_GATE_LEASE_EXPIRED"
REASON_GENERATION_MISMATCH = "WRITE_GATE_GENERATION_MISMATCH"
REASON_REDIS_UNAVAILABLE = "WRITE_GATE_REDIS_UNAVAILABLE"
REASON_DISABLED_BY_POLICY = "WRITE_GATE_PROVIDER_DISABLED"
#: 代际为 0 / 代际键已过期：**没有权威代际可比较**，等于"版本无法确认" → 阻断。
REASON_GENERATION_UNKNOWN = "WRITE_GATE_GENERATION_UNKNOWN"

#: 全局写租约 scope。写路径与运维面板**必须**用同一个值：
#: 曾经出现 `default` / `workspace` 两套 scope 的风险——运维在 `default` 上续签，
#: 而执行检查的是 `workspace`，结果是"续签了却写不进去"（或更糟：没续签却放行了）。
#: 这里把它定成唯一常量，任何新调用点都必须引它，不再手写字符串。
WRITE_SCOPE = "workspace"


@dataclass(slots=True)
class WriteLease:
    """一条写租约（本地缓存条目）。"""

    scope: str = "default"
    generation: int = 0
    granted_at: float = 0.0
    lease_ttl: int = 0
    #: 授权来源（``bootstrap`` / ``admin`` / ``provider``…），仅审计用。
    source: str = ""

    def age(self, *, now: float | None = None) -> float:
        moment = time.time() if now is None else float(now)
        return max(0.0, moment - float(self.granted_at or 0.0))

    def is_fresh(self, *, now: float | None = None) -> bool:
        ttl = int(self.lease_ttl or lease_ttl_seconds())
        return self.age(now=now) <= max(1, ttl)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "generation": int(self.generation),
            "granted_at": float(self.granted_at),
            "lease_ttl": int(self.lease_ttl),
            "source": self.source,
        }


@dataclass(slots=True)
class WriteGate:
    """写闸：本地租约缓存 + Redis 代际校验。

    ``_redis_factory`` 可注入（测试）；生产用 ``app.core.redis.get_redis``。
    """

    _redis_factory: Any = None
    _leases: dict[str, WriteLease] = field(default_factory=dict)
    _checks: int = 0
    _denials: int = 0
    _last_reason: str = ""

    # ── Redis ──────────────────────────────────────────────

    def _redis(self) -> Any | None:
        try:
            if self._redis_factory is not None:
                return self._redis_factory()
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001 - 未初始化/不可用都按"不可达"处理（写侧阻断）
            return None

    # ── 授权（Admin / Provider 注册 / 启动引导）─────────────

    def grant(
        self,
        *,
        scope: str = WRITE_SCOPE,
        generation: int | None = None,
        lease_ttl: int | None = None,
        source: str = "",
    ) -> WriteLease:
        """在本地记下一份写授权（并返回它）。

        ``generation`` 缺省沿用当前 Redis 代际；Redis 不可达时用 ``0``，
        此时 :meth:`check` 会因"没有权威代际"直接阻断——绝不因为"授权时 Redis 就不可用"
        而给出一个看起来有效的租约。
        """
        name = str(scope or WRITE_SCOPE)
        lease = WriteLease(
            scope=name,
            generation=int(generation if generation is not None else 0),
            granted_at=time.time(),
            lease_ttl=int(lease_ttl if lease_ttl is not None else lease_ttl_seconds()),
            source=str(source or ""),
        )
        self._leases[name] = lease
        return lease

    async def grant_async(
        self,
        *,
        scope: str = WRITE_SCOPE,
        lease_ttl: int | None = None,
        source: str = "",
    ) -> WriteLease:
        """读当前代际并授权（写侧唯一推荐的授权入口）。

        Redis 不可达 / 代际读不到（==0）时**抛错**而不是发一个 generation=0 的租约：
        那种租约在下一次 :meth:`check` 会被判成"代际未知"，看起来像"授权成功了但写不
        进去"——还不如在授权这一步就把问题说清楚（Fail-Closed 的语义要一致）。
        """
        if self._redis() is None:
            raise WriteGateDenied(REASON_REDIS_UNAVAILABLE, "Redis 不可用，无法签发写租约")
        generation = await self._read_generation(scope)
        if generation <= 0:
            raise WriteGateDenied(
                REASON_GENERATION_UNKNOWN, "读不到权威写代际（键缺失/为 0），无法签发写租约"
            )
        return self.grant(
            scope=scope, generation=generation, lease_ttl=lease_ttl, source=source
        )

    async def revoke(self, *, scope: str = WRITE_SCOPE) -> int:
        """撤销本地租约并推进 Redis 代际（其它进程下一次检查就会阻断）。"""
        self._leases.pop(str(scope or WRITE_SCOPE), None)
        return await self.bump_generation(scope)

    async def bump_generation(self, scope: str = WRITE_SCOPE) -> int:
        """推进代际（管理员改绑/撤销/停用 Provider 时调用）。"""
        client = self._redis()
        key = generation_key(scope)
        if client is None:
            # Redis 不可达时**不改本地代际**：让所有进程继续按各自缓存的代际判定，
            # 而它们下一次 check 会因 Redis 不可达而阻断（Fail-Closed）。
            raise WriteGateDenied(REASON_REDIS_UNAVAILABLE, "Redis 不可用，无法推进写代际")
        generation = int(await client.incr(key))
        await client.expire(key, generation_ttl_seconds())
        # 本进程的租约同步到新代际（否则刚授权的进程会立刻把自己判成不一致）。
        name = str(scope or WRITE_SCOPE)
        for lease in self._leases.values():
            if lease.scope == name:
                lease.generation = generation
        return generation

    async def _read_generation(self, scope: str) -> int:
        client = self._redis()
        if client is None:
            return 0
        try:
            raw = await client.get(generation_key(scope))
            return int(str(raw or "0") or "0")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[write-gate] 代际读取失败: {}", str(exc)[:120])
            return 0

    # ── 校验（写路径唯一入口）───────────────────────────────

    async def check(self, *, scope: str = WRITE_SCOPE, required: bool | None = None) -> WriteLease:
        """写操作前的闸门：通过返回租约，否则抛 :class:`WriteGateDenied`。

        ``required=False`` 时（或开关关闭）直接放行，返回一个"未启用"占位租约，
        让调用方代码只有一条路径。

        判定顺序即"越可疑越早拒绝"：没有缓存 → 代际未知 → 租约过期 → Redis 不可达
        → 代际不一致。**任何一步无法确认都阻断**（写侧 Fail-Closed）。
        """
        enforced = write_gate_enabled() if required is None else bool(required)
        if not enforced:
            return WriteLease(scope=str(scope or WRITE_SCOPE), source="disabled")
        self._checks += 1
        name = str(scope or WRITE_SCOPE)
        lease = self._leases.get(name)
        if lease is None:
            raise self._deny(REASON_NO_CACHE, "本进程没有该写授权的缓存，已阻断写操作")
        # 代际为 0 = 从没读到过权威代际（授权时 Redis 就不可用）。没有权威版本可比较
        # 就**不能**放行：那种租约看起来有效，实际上无法证明"期间没有状态变更"。
        if int(lease.generation or 0) <= 0:
            raise self._deny(
                REASON_GENERATION_UNKNOWN,
                "写租约没有权威代际（授权时未能读到），无法确认状态未变更，已阻断写操作",
            )
        if not lease.is_fresh():
            raise self._deny(
                REASON_LEASE_EXPIRED,
                f"写授权已超过 {int(lease.lease_ttl or lease_ttl_seconds())}s 未确认，已阻断写操作",
            )
        client = self._redis()
        if client is None:
            raise self._deny(REASON_REDIS_UNAVAILABLE, "Redis 不可用，已阻断写操作")
        try:
            raw = await client.get(generation_key(name))
            if raw is None:
                # 代际键**过期/不存在** → 权威版本已消失。继续放行等于把"TTL 到期"
                # 当成"授权无限期有效"，所以阻断（需要运维重新续签）。
                raise self._deny(
                    REASON_GENERATION_UNKNOWN,
                    "写代际键已过期或不存在，无法确认当前授权状态，已阻断写操作",
                )
            current = int(str(raw) or "0")
        except WriteGateDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - Redis 报错也阻断（保守）
            logger.warning("[write-gate] 代际校验失败（阻断写）: {}", str(exc)[:120])
            raise self._deny(REASON_REDIS_UNAVAILABLE, "Redis 代际校验失败，已阻断写操作") from exc
        if current <= 0:
            raise self._deny(
                REASON_GENERATION_UNKNOWN, "写代际无效（<=0），已阻断写操作"
            )
        stored = int(lease.generation or 0)
        if current != stored:
            raise self._deny(
                REASON_GENERATION_MISMATCH,
                f"写代际不一致（本地 {stored} ≠ 权威 {current}），已阻断写操作",
            )
        # 代际一致：刷新确认时刻（缓存新鲜度是"最近一次成功确认"起算的）。
        lease.granted_at = time.time()
        lease.generation = current
        return lease

    def is_write_allowed(self, *, scope: str = WRITE_SCOPE) -> bool:
        """**同步**只读判定（不碰 Redis）：用于"能不能进写流程"的快速预判。

        真正的放行判定必须走 :meth:`check`（它才做代际校验）。这里返回 ``True``
        只代表"本地缓存看起来新鲜且代际已知"，不代表已授权。
        """
        if not write_gate_enabled():
            return True
        lease = self._leases.get(str(scope or WRITE_SCOPE))
        return bool(lease is not None and lease.generation > 0 and lease.is_fresh())

    def _deny(self, reason: str, message: str) -> WriteGateDenied:
        self._denials += 1
        self._last_reason = str(reason or "")
        logger.warning("[write-gate] 拒绝写操作 reason={} scope={}", reason, self._last_reason_scope())
        return WriteGateDenied(reason, message)

    def _last_reason_scope(self) -> str:
        return ",".join(sorted(self._leases)) or "-"

    # ── 观测 ────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": write_gate_enabled(),
            "checks": int(self._checks),
            "denials": int(self._denials),
            "last_reason": self._last_reason,
            "lease_ttl_seconds": int(lease_ttl_seconds()),
            "leases": [
                {**lease.as_dict(), "age_seconds": round(lease.age(now=now), 3), "fresh": lease.is_fresh(now=now)}
                for lease in sorted(self._leases.values(), key=lambda item: item.scope)
            ],
        }

    def clear_for_tests(self) -> None:
        self._leases.clear()
        self._checks = 0
        self._denials = 0
        self._last_reason = ""


#: 进程内单例（Provider 注册/Admin API 与写路径共用同一份缓存）。
write_gate = WriteGate()


__all__ = [
    "GENERATION_KEY_PREFIX",
    "REASON_DISABLED_BY_POLICY",
    "REASON_GENERATION_MISMATCH",
    "REASON_GENERATION_UNKNOWN",
    "REASON_LEASE_EXPIRED",
    "REASON_NO_CACHE",
    "REASON_REDIS_UNAVAILABLE",
    "WRITE_SCOPE",
    "WriteGate",
    "WriteGateDenied",
    "WriteLease",
    "generation_key",
    "generation_ttl_seconds",
    "lease_ttl_seconds",
    "write_gate",
    "write_gate_enabled",
]
