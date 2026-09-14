"""阶段 2：Provider 租约服务（注册 / 心跳 / 注销 / 过期即摘除）。

租约是"此刻确实有一个 Provider 能提供某个能力"的唯一证据。本服务负责维护它，并
**同步**能力注册表：租约失效就摘除能力，注册/续期就恢复能力——不能让 Broker 从
"配置里写了这个 Provider"推断可用性。

存储：进程内（与准入 ``lumi_orch.admission`` 的进程内回退同一风格）。多 API worker
共享时租约需要 Redis 承载；这里把读写收敛在 ``_load`` / ``_store`` 两个钩子里，届时
只换实现，不改调用方。

过期处理是**惰性**的（``purge_expired`` 在查询与快照时调用），并且每次变更都向能力
事件流发布 ``provider_connected`` / ``provider_disconnected``，
这样"客户端断开"在气泡里是结构化状态而不是"工具调用失败"。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    ACTIVATABLE_PLUGIN_KINDS,
    CapabilityErrorCode,
    CapabilityResult,
    Deployment,
    PluginKind,
    ProviderHealth,
    ProviderLease,
    TrustLevel,
    capability_failure,
)

from app.agents.capabilities.catalog.legacy import CapabilityCatalog, capability_catalog
from app.agents.capabilities.registry.registry import CapabilityRegistry, capability_registry
from app.services.capability_lease_redis import lease_id_for
from app.services.capability_events import (
    CAPABILITY_EVENT_PROVIDER_CONNECTED,
    CAPABILITY_EVENT_PROVIDER_DISCONNECTED,
    CAPABILITY_EVENT_PLUGIN_HEALTH_CHANGED,
    publish_capability_event,
)

#: 默认租约时长（秒）；心跳按 1/3 间隔续期即可安全。
DEFAULT_LEASE_TTL_SECONDS = 120.0
#: 允许的租约区间：过短会因网络抖动频繁摘除，过长会让离线 Provider 长时间仍被选中。
MIN_LEASE_TTL_SECONDS = 5.0
MAX_LEASE_TTL_SECONDS = 3600.0


class LeaseRejected(ValueError):
    """租约被拒绝（能力未声明 / 位置不允许 / 主体不符）。带稳定错误码。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = str(code)
        self.message = str(message or code)
        self.details = dict(details or {})
        super().__init__(self.message)

    def to_result(self, *, capability: str = "", provider_id: str = "") -> CapabilityResult:
        return capability_failure(
            self.code,
            self.message,
            capability=capability,
            provider_id=provider_id,
            retryable=False,
            details=self.details,
        )


def _now() -> float:
    return time.time()


def _worker_id() -> str:
    """本进程的 worker 标识（健康记录里用它区分是哪个 worker 看到的最后一次心跳）。"""
    import os

    return f"pid-{os.getpid()}"


def clamp_ttl(ttl: Any) -> float:
    try:
        value = float(ttl)
    except (TypeError, ValueError):
        value = DEFAULT_LEASE_TTL_SECONDS
    if value <= 0:
        value = DEFAULT_LEASE_TTL_SECONDS
    return min(max(value, MIN_LEASE_TTL_SECONDS), MAX_LEASE_TTL_SECONDS)


class CapabilityLeaseService:
    """租约的签发、续期、注销与过期清理（唯一写入方）。"""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry | None = None,
        catalog: CapabilityCatalog | None = None,
        provider_resolver: Callable[[ProviderLease], Any] | None = None,
        redis_registry: Any = None,
    ) -> None:
        self._registry = registry or capability_registry
        self._catalog = catalog or capability_catalog
        # (capability@version, provider_id) → 租约
        self._leases: dict[tuple[str, str], ProviderLease] = {}
        # 注册表只需要"能力 → 实现"的映射；真正的实现由调用方（Broker/内置 Provider）
        # 注入。客户端 Provider 走事件流/HTTP 转发，不需要服务端持有实现对象。
        self._provider_resolver = provider_resolver or _placeholder_provider
        # 跨 worker：Redis 是权威副本，本进程缓存供 Broker 同步选路。
        if redis_registry is None:
            from app.services.capability_lease_redis import RedisLeaseRegistry

            redis_registry = RedisLeaseRegistry()
        self._redis_registry = redis_registry

    # ── 查询 ──────────────────────────────────────────────

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    def snapshot(self, *, purge: bool = True) -> list[ProviderLease]:
        """全部活租约（本进程 + 其它 worker 的）。

        Redis 可用时以**权威副本**为准；不可用（未初始化/宕机）时退回进程内字典，
        单 worker 部署与测试因此不受影响。

        **进程内字典永远并入结果**（不是 Redis 为空时才回退）：注册/心跳会先写进程内
        副本再写 Redis，若某次 Redis 读缓存刚好为空，只有进程内副本的 worker 会把
        "自己刚注册的租约"读丢 —— Broker 的能力选择因此恒判"没有可用的 Provider"。
        反之亦然：Redis 读缓存里带着其它 worker 的租约，这是跨 worker 唯一的可见性来源。
        """
        merged: dict[tuple[str, str], ProviderLease] = {}
        try:
            for lease in self._redis_registry.snapshot_cached():
                merged[(lease.qualified_capability, lease.provider_id)] = lease
        except Exception as exc:  # noqa: BLE001 - Redis 读失败不影响进程内副本
            logger.debug("[capability] Redis 租约缓存读取失败（降级为进程内视图）: {}", str(exc)[:120])
        if purge:
            self.purge_expired()
        for key, lease in self._leases.items():
            merged.setdefault(key, lease)
        return list(merged.values())

    async def refresh_from_redis(self) -> list[ProviderLease]:
        """从 Redis 拉取权威租约（跨 worker 可见性的显式同步点）。"""
        leases = await self._redis_registry.refresh()
        if leases:
            self._redis_registry.seed_cache(leases)
        return leases

    def invalidate_redis_cache(self) -> None:
        """清空本地租约读缓存（收到撤销广播时调用）。

        只清缓存、**不动权威数据**：下次 ``refresh_from_redis`` 会从 Redis 重建。
        这样"撤销广播丢了"最坏是多一次 Redis 往返，而不是把已撤销的租约当活的用。
        """
        self._redis_registry.clear_cache()

    def get(self, *, capability: str, provider_id: str) -> ProviderLease | None:
        base = str(capability or "").split("@", 1)[0]
        for (qualified, pid), lease in self._leases.items():
            if pid != str(provider_id or ""):
                continue
            if qualified.split("@", 1)[0] == base:
                return lease
        return None

    def leases_for(self, capability: str, *, version: int | None = None) -> list[ProviderLease]:
        base = str(capability or "").split("@", 1)[0]
        rows: list[ProviderLease] = []
        for lease in self._leases.values():
            if lease.capability != base:
                continue
            if version is not None and int(lease.contract_version) != int(version):
                continue
            rows.append(lease)
        return rows

    # ── 写入 ──────────────────────────────────────────────

    async def register(
        self,
        *,
        provider_id: str,
        capabilities: list[dict[str, Any]] | list[str],
        user_id: str,
        device_id: str = "",
        conversation_id: str = "",
        workspace_id: str = "",
        session_id: str = "",
        deployment: str = Deployment.CLIENT.value,
        trust_level: str = TrustLevel.OFFICIAL.value,
        plugin_id: str = "",
        plugin_version: str = "",
        provider_version: str = "",
        scope: dict[str, Any] | None = None,
        ttl_seconds: Any = None,
        health_status: str = ProviderHealth.UNKNOWN.value,
        job_id: str = "",
        execution_plane: str = "",
        runtime_kind: str = "",
    ) -> list[ProviderLease]:
        """签发（或覆盖）一个 Provider 的若干能力租约。

        注册主体必须由**鉴权结果**给出（``user_id`` 来自 token，不接受客户端自述），
        能力必须在目录里已声明，且注册位置必须满足该能力的数据本地性。

        ``execution_plane`` / ``runtime_kind`` 为客户端可选上报（缺省按 ``deployment``
        推导）：租约要能回答"这次到底谁在执行、用什么运行方式"，而不是只写一个混用词表。
        """
        pid = str(provider_id or "").strip()
        if not pid:
            raise LeaseRejected(
                CapabilityErrorCode.INVALID_ARGUMENTS.value, "provider_id 不能为空"
            )
        owner = str(user_id or "").strip()
        if not owner:
            raise LeaseRejected(
                CapabilityErrorCode.PERMISSION_DENIED.value, "无法确定注册主体（未鉴权）"
            )
        site = self._parse_deployment(deployment)
        trust = self._parse_trust(trust_level)
        ttl = clamp_ttl(ttl_seconds)
        stamps = self._timestamp(ttl)
        # 心跳健康值只允许"活着"的取值：注册时自称 unhealthy 没有意义。
        health = self._parse_health(health_status, default=ProviderHealth.UNKNOWN)

        items = self._normalize_capabilities(capabilities)
        if not items:
            raise LeaseRejected(
                CapabilityErrorCode.INVALID_ARGUMENTS.value, "capabilities 不能为空"
            )
        issued: list[ProviderLease] = []
        for item in items:
            name = str(item.get("capability") or item.get("name") or "").strip()
            version = int(item.get("contract_version") or item.get("version") or 1)
            descriptor = self._catalog.get(name, version=version)
            if descriptor is None:
                raise LeaseRejected(
                    CapabilityErrorCode.CAPABILITY_MISSING.value,
                    f"未声明的能力：{name}@{version}",
                    details={"capability": f"{name}@{version}"},
                )
            if not descriptor.allows_registration(site):
                # 本地能力注册到服务端 = 把本地数据搬到云端，必须拒绝。
                # （hybrid 两侧都允许注册；调用期是否切换由 Broker + 策略决定。）
                raise LeaseRejected(
                    CapabilityErrorCode.DEPLOYMENT_NOT_ALLOWED.value,
                    f"能力 {descriptor.qualified_name} 的数据本地性 "
                    f"{descriptor.data_locality} 不允许在 {site} 侧注册",
                    details={
                        "capability": descriptor.qualified_name,
                        "data_locality": str(descriptor.data_locality),
                        "deployment": str(site),
                    },
                )
            lease = ProviderLease(
                provider_id=pid,
                capability=descriptor.name,
                contract_version=descriptor.contract_version,
                # 租约主键稳定派生：同一 Provider 同一能力重注册即覆盖同一条。
                lease_id=lease_id_for(pid, descriptor.name),
                user_id=owner,
                device_id=str(device_id or ""),
                conversation_id=str(conversation_id or ""),
                workspace_id=str(workspace_id or ""),
                session_id=str(session_id or ""),
                deployment=site,
                # 显式上报优先；缺省 None → 由 ProviderLease 按 deployment 推导。
                execution_plane=str(execution_plane or "") or None,
                runtime_kind=str(runtime_kind or "") or None,
                trust_level=trust,
                plugin_id=str(plugin_id or ""),
                plugin_version=str(plugin_version or ""),
                provider_version=str(provider_version or ""),
                scope=dict(scope or {}),
                health_status=health,
                issued_at=stamps[0],
                expires_at=stamps[1],
                last_heartbeat_at=stamps[0],
            )
            self._leases[(lease.qualified_capability, pid)] = lease
            issued.append(lease)
            await publish_capability_event(
                CAPABILITY_EVENT_PROVIDER_CONNECTED,
                job_id=job_id or str(scope.get("job_id") if scope else ""),
                capability=lease.qualified_capability,
                provider_id=pid,
                provider_version=lease.provider_version,
                contract_version=lease.contract_version,
                device_id=lease.device_id,
                workspace_id=lease.workspace_id,
                conversation_id=lease.conversation_id,
                plugin_id=lease.plugin_id,
                plugin_version=lease.plugin_version,
                status="idle",
                health_status=str(lease.health_status),
                execution_plane=str(lease.plane()),
                runtime_kind=str(lease.runtime()),
                executor_type=lease.executor_type(),
            )
        # 跨 worker 可见性：写进 Redis 权威副本（Redis 不可用时只保留本地，返回值可判）。
        published = await self._redis_registry.publish_leases(
            issued,
            health={
                "provider_id": pid,
                "status": str(health),
                "last_seen_at": stamps[0],
                "worker_id": _worker_id(),
            },
        )
        if published:
            # 只有写入成功才更新全局读缓存：否则会把"本 worker 看到的新租约"当成全局
            # 事实，在其它 worker 上造成"看得到但派发不到"的假象。
            self._redis_registry.seed_cache(issued)
        logger.info(
            "[capability] 租约注册 provider={} capabilities={} ttl={}s device={} redis={}",
            pid, [item.qualified_capability for item in issued], int(ttl),
            device_id or "-", published,
        )
        return issued

    async def heartbeat(
        self,
        *,
        provider_id: str,
        user_id: str,
        capabilities: list[dict[str, Any]] | list[str] | None = None,
        ttl_seconds: Any = None,
        health_status: str = "",
        job_id: str = "",
        execution_plane: str = "",
        runtime_kind: str = "",
    ) -> list[ProviderLease]:
        """续期：未过期的租约延长存活；已过期的不复活（必须重新注册）。

        返回**实际续期成功**的租约。返回空列表表示"这次心跳没有任何东西需要续期"
        （例如客户端刚撤销了全部能力）——这是合法状态，不是错误；调用方若需要区分
        "请求了却一个都没续上"，用返回的列表是否为空自行判断。

        ``execution_plane`` / ``runtime_kind`` 给了就更新（插件可能从进程内迁到 Worker），
        没给则沿用租约里已有的值。
        """
        pid = str(provider_id or "").strip()
        owner = str(user_id or "").strip()
        ttl = clamp_ttl(ttl_seconds)
        now = _now()
        # 客户端会把能力写成 ``workspace.read@1``，而租约里存的是**基名**
        # （``workspace.read``，版本在 ``contract_version``）。这里统一取基名，
        # 否则续期会被自己的过滤条件全部排除（表现为"心跳成功但什么都没续上"）。
        wanted = {
            str(item.get("capability") or item.get("name") or "").split("@", 1)[0].strip()
            for item in self._normalize_capabilities(capabilities or [])
        }
        renewed: list[ProviderLease] = []
        for (qualified, key_pid), lease in list(self._leases.items()):
            if key_pid != pid:
                continue
            if str(lease.user_id or "") != owner:
                # 不能替别人的 Provider 续期。
                continue
            if wanted and lease.capability not in wanted:
                continue
            if lease.is_expired(now=now):
                # 过期后靠心跳复活会绕过"重新注册（含重新声明能力）"这一步。
                continue
            updated = lease.renew(ttl_seconds=ttl, now=now)
            if health_status:
                updated = updated.model_copy(
                    update={"health_status": self._parse_health(health_status)}
                )
                await publish_capability_event(
                    CAPABILITY_EVENT_PLUGIN_HEALTH_CHANGED,
                    job_id=job_id,
                    capability=updated.qualified_capability,
                    provider_id=pid,
                    health_status=str(updated.health_status),
                    contract_version=updated.contract_version,
                )
            # 执行位置/运行方式可以在心跳里纠正（缺省沿用租约现值）。
            # 注意必须显式解析成枚举：model_copy 不做校验，塞字符串会让后续
            # `is ExecutionPlane.CLIENT` 之类的判断静默失败。
            plane = str(execution_plane or "").strip()
            runtime = str(runtime_kind or "").strip()
            if plane or runtime:
                from lumi_contracts.plugins import parse_execution_plane, parse_runtime_kind

                updated = updated.model_copy(
                    update={
                        "execution_plane": (
                            parse_execution_plane(plane) if plane else updated.plane()
                        ),
                        "runtime_kind": (
                            parse_runtime_kind(runtime) if runtime else updated.runtime()
                        ),
                    }
                )
            self._leases[(qualified, pid)] = updated
            renewed.append(updated)
        return renewed

    async def unregister(
        self,
        *,
        provider_id: str,
        user_id: str = "",
        capability: str = "",
        job_id: str = "",
    ) -> list[ProviderLease]:
        """注销（客户端关闭/禁用插件/退出登录）；返回被摘除的租约。"""
        pid = str(provider_id or "").strip()
        base = str(capability or "").split("@", 1)[0]
        removed: list[ProviderLease] = []
        for key in list(self._leases):
            qualified, key_pid = key
            if key_pid != pid:
                continue
            if base and qualified.split("@", 1)[0] != base:
                continue
            lease = self._leases[key]
            if user_id and str(lease.user_id or "") != str(user_id):
                continue
            removed.append(self._leases.pop(key))
        for lease in removed:
            await publish_capability_event(
                CAPABILITY_EVENT_PROVIDER_DISCONNECTED,
                job_id=job_id,
                capability=lease.qualified_capability,
                provider_id=lease.provider_id,
                contract_version=lease.contract_version,
                device_id=lease.device_id,
                health_status=str(ProviderHealth.OFFLINE),
                status="unavailable",
                error_code=CapabilityErrorCode.PROVIDER_OFFLINE.value,
            )
        if removed:
            # 跨 worker：删 Redis 权威副本 + 广播，让别的 worker 立刻失效读缓存，
            # 不然"撤销了但别的 worker 还在派发"会有窗口期。
            await self._redis_registry.remove_leases(removed)
            from app.services.capability_revoke import broadcast_revoke

            for lease in removed:
                await broadcast_revoke(
                    capability=lease.qualified_capability,
                    provider_id=lease.provider_id,
                    plugin_id=lease.plugin_id,
                    lease_id=lease.lease_id,
                    reason="provider_unregistered",
                )
            logger.info(
                "[capability] 租约注销 provider={} capabilities={}",
                pid, [item.qualified_capability for item in removed],
            )
        return removed

    def purge_expired(self, *, now: float | None = None) -> list[ProviderLease]:
        """清理过期租约（惰性调用；返回被清理的条目）。"""
        stamp = _now() if now is None else float(now)
        expired: list[ProviderLease] = []
        for key, lease in list(self._leases.items()):
            if lease.is_expired(now=stamp):
                expired.append(self._leases.pop(key))
        return expired

    # ── 注册表同步 ────────────────────────────────────────

    def sync_registry(self, *, now: float | None = None) -> list[str]:
        """把租约状态同步到能力注册表：过期/断线的 Provider 被摘除能力。

        返回被摘除的 ``provider_id``（去重）。Broker 只从注册表找实现，因此这里是
        "客户端断线 ⇒ 能力不可用"的唯一落点；活着的租约在这里（重新）注册，
        使其描述符进入注册表供 Broker 解析。
        """
        stamp = _now() if now is None else float(now)
        self.purge_expired(now=stamp)
        dropped: list[str] = []
        for registration in self._registry.providers():
            pid = registration.provider_id
            leases = [lease for lease in self._leases.values() if lease.provider_id == pid]
            if not leases or all(
                lease.health_status is ProviderHealth.OFFLINE for lease in leases
            ):
                self._registry.unregister(pid)
                dropped.append(pid)
        for pid in {lease.provider_id for lease in self._leases.values()} - {
            item.provider_id for item in self._registry.providers()
        }:
            self._register_lease_provider(pid)
        return dropped

    def _register_lease_provider(self, provider_id: str) -> None:
        """把某个 Provider 的活租约登记进能力注册表（描述符取自目录）。"""
        leases = [lease for lease in self._leases.values() if lease.provider_id == provider_id]
        if not leases:
            return
        head = leases[0]
        descriptors: list[Any] = []
        for lease in leases:
            descriptor = self._catalog.get(lease.capability, version=lease.contract_version)
            if descriptor is not None and descriptor not in descriptors:
                descriptors.append(descriptor)
        if not descriptors:
            return
        try:
            self._registry.register(
                self._provider_resolver(head),
                descriptors=tuple(descriptors),
                deployment=head.deployment,
                trust_level=head.trust_level,
                plugin_id=head.plugin_id,
                plugin_version=head.plugin_version,
                provider_version=head.provider_version,
                health_status=head.health_status,
                # 租约里的实际执行位置/运行方式同步进注册表（不是重新猜 deployment）。
                execution_plane=head.plane(),
                runtime_kind=head.runtime(),
            )
        except ValueError as exc:  # noqa: BLE001 - 注册约束不满足时不进入注册表
            logger.warning(
                "[capability] Provider {} 未进入注册表: {}", provider_id, str(exc)[:160]
            )

    # ── 内部 ──────────────────────────────────────────────

    def _timestamp(self, ttl: float) -> tuple[float, float]:
        now = _now()
        return now, now + ttl

    @staticmethod
    def _parse_deployment(value: Any) -> Deployment:
        key = str(getattr(value, "value", value) or "").strip().casefold()
        for item in Deployment:
            if item.value == key:
                return item
        raise LeaseRejected(
            CapabilityErrorCode.INVALID_ARGUMENTS.value, f"未知部署位置：{value!r}"
        )

    @staticmethod
    def _parse_trust(value: Any) -> TrustLevel:
        key = str(getattr(value, "value", value) or "").strip().casefold()
        for item in TrustLevel:
            if item.value == key:
                return item
        return TrustLevel.THIRD_PARTY

    @staticmethod
    def _parse_health(value: Any, *, default: ProviderHealth = ProviderHealth.UNKNOWN) -> ProviderHealth:
        key = str(getattr(value, "value", value) or "").strip().casefold()
        for item in ProviderHealth:
            if item.value == key:
                return item
        return default

    @staticmethod
    def _normalize_capabilities(
        capabilities: list[dict[str, Any]] | list[str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in capabilities or []:
            if isinstance(item, str):
                rows.append({"capability": item})
            elif isinstance(item, dict):
                rows.append(dict(item))
        return rows

    # ── 审计快照 ──────────────────────────────────────────

    def plugin_snapshot(self) -> dict[str, Any]:
        """租约视角的快照（进 Job.run_view 的 ``plugin_snapshot``）。"""
        from lumi_contracts.plugins import PluginSnapshot

        return PluginSnapshot.from_leases(
            leases=self.snapshot(), capabilities=list(self._catalog.all())
        ).to_snapshot()


class _PlaceholderProvider:
    """没有服务端实现对象的 Provider 占位（客户端 Provider / 事件流转发）。

    注册表需要"谁提供这个能力"的索引；客户端 Provider 的实现不在服务端进程里，
    因此这里只提供身份与描述符，``invoke`` 明确失败——真正的转发由 Broker 完成，
    绝不允许静默落回服务端执行本地能力。
    """

    def __init__(self, lease: ProviderLease) -> None:
        self._provider_id = lease.provider_id
        self._deployment = lease.deployment

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def deployment(self) -> Deployment:
        return self._deployment

    @property
    def descriptors(self) -> tuple[Any, ...]:
        return ()

    async def invoke(self, invocation: Any, *, context: Any) -> CapabilityResult:
        # 语义必须诚实：这是"客户端能力，服务端不是执行方"，而不是"客户端掉线"。
        # 标 retryable=False，避免调用方把它当成可重试的临时故障反复重发。
        # 真正的派发走能力路由（executor → CapabilityDispatchAdapter → MCP）。
        return capability_failure(
            CapabilityErrorCode.CAPABILITY_UNAVAILABLE,
            "该能力由客户端 Provider 提供，服务端不是执行方（请经能力派发通道调用）",
            capability=str(getattr(invocation, "qualified_capability", "")),
            provider_id=self._provider_id,
            retryable=False,
        )


def _placeholder_provider(lease: ProviderLease) -> Any:
    return _PlaceholderProvider(lease)


def validate_plugin_kind_activatable(kind: Any, *, developer_mode: bool = False) -> PluginKind:
    """插件类型是否允许激活（未知类型默认拒绝；Extension Handler 需开发者模式）。

    放在租约服务旁边，是因为"未知 kind 不允许执行"必须在注册入口生效，而不是等到
    调用时才失败。
    """
    from lumi_contracts.plugins import parse_plugin_kind

    parsed = parse_plugin_kind(kind)
    if parsed is None:
        raise LeaseRejected(
            CapabilityErrorCode.UNKNOWN_PLUGIN_KIND.value,
            f"未知插件类型：{kind!r}（需要注册 Extension Handler 后才允许）",
        )
    if parsed.value not in ACTIVATABLE_PLUGIN_KINDS and not developer_mode:
        raise LeaseRejected(
            CapabilityErrorCode.UNKNOWN_PLUGIN_KIND.value,
            f"插件类型 {parsed.value} 只能在开发者模式注册",
            details={"kind": parsed.value},
        )
    return parsed


__all__ = [
    "CapabilityLeaseService",
    "DEFAULT_LEASE_TTL_SECONDS",
    "LeaseRejected",
    "MAX_LEASE_TTL_SECONDS",
    "MIN_LEASE_TTL_SECONDS",
    "capability_lease_service",
    "clamp_ttl",
    "validate_plugin_kind_activatable",
]


#: 进程内共享租约服务（注册端点与撤销订阅必须用**同一个**实例，否则清缓存清不到）。
capability_lease_service = CapabilityLeaseService()
