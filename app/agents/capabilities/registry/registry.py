"""阶段 1：Capability Provider 协议与注册表。

Provider 是"能做什么"的实现者：它拿到一次 :class:`CapabilityInvocation` 与只读的
:class:`AgentExecutionContext`，返回统一的 :class:`CapabilityResult`。它**不**决定：

* 这次调用允不允许（审批与策略在 Broker/Policy 层）；
* 应该路由到哪个 Provider（选择在 Broker）；
* 参数是否越权（工作区/项目从上下文取，模型传值被忽略）。

注册表只认"目录里已声明、且注册声明与目录一致"的能力（见
``CapabilityCatalog.assert_catalog_consistent``），避免 Provider 用自定义声明把自己
说成另一个位置/去掉审批。同一 ``(能力, 版本)`` 在同一绑定下只保留一个 Provider，
重复注册是**替换**而不是叠加。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityInvocation,
    CapabilityResult,
    Deployment,
    ExecutionPlane,
    ProviderHealth,
    RuntimeKind,
    TrustLevel,
    capability_failure,
    executor_type_for,
    parse_execution_plane,
    parse_runtime_kind,
    plane_for_deployment,
)

from app.agents.capabilities.catalog.legacy import (
    CapabilityCatalog,
    capability_catalog,
)
from app.agents.capabilities.contracts.context import AgentExecutionContext

# 部署位置判定（纯决策）已迁到 backend-neutral 的 ``lumi_capability.deployment``。
from lumi_capability.deployment import descriptor_allows_deployment as _pkg_descriptor_allows_deployment


@runtime_checkable
class CapabilityProvider(Protocol):
    """能力实现者（服务端内置 Provider 与客户端 Provider 共用这一形状）。"""

    @property
    def provider_id(self) -> str: ...

    @property
    def deployment(self) -> Any: ...

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]: ...

    async def invoke(
        self,
        invocation: CapabilityInvocation,
        *,
        context: AgentExecutionContext,
    ) -> CapabilityResult: ...


class ProviderRegistration:
    """一个已注册的 Provider（描述符 + 元数据，不持执行状态）。"""

    __slots__ = (
        "provider",
        "provider_id",
        "deployment",
        "execution_plane",
        "runtime_kind",
        "trust_level",
        "plugin_id",
        "plugin_version",
        "provider_version",
        "descriptors",
        "health_status",
    )

    def __init__(
        self,
        *,
        provider: CapabilityProvider,
        provider_id: str,
        deployment: Deployment,
        descriptors: tuple[CapabilityDescriptor, ...],
        trust_level: TrustLevel = TrustLevel.BUILTIN,
        plugin_id: str = "",
        plugin_version: str = "",
        provider_version: str = "",
        health_status: ProviderHealth = ProviderHealth.UNKNOWN,
        execution_plane: ExecutionPlane | str | None = None,
        runtime_kind: RuntimeKind | str | None = None,
    ) -> None:
        self.provider = provider
        self.provider_id = str(provider_id)
        self.deployment = deployment
        #: 注册方声明的执行位置 / 运行方式（缺省按 deployment 推导）。
        self.execution_plane = (
            parse_execution_plane(execution_plane) if execution_plane else plane_for_deployment(deployment)
        )
        self.runtime_kind = (
            parse_runtime_kind(runtime_kind) if runtime_kind else RuntimeKind.IN_PROCESS
        )
        self.descriptors = descriptors
        self.trust_level = trust_level
        self.plugin_id = str(plugin_id)
        self.plugin_version = str(plugin_version)
        self.provider_version = str(provider_version)
        self.health_status = health_status

    def executor_type(self) -> str:
        """兼容旧字段的派生值（``server``/``client``/``worker``/``container``/``sandbox``）。"""
        return executor_type_for(self.execution_plane, self.runtime_kind)

    def to_snapshot(self) -> dict[str, Any]:
        """注册项的机器可读快照（`/capabilities` 与审计都读它）。"""
        return {
            "provider_id": self.provider_id,
            "deployment": str(self.deployment),
            "execution_plane": str(self.execution_plane),
            "runtime_kind": str(self.runtime_kind),
            "executor_type": self.executor_type(),
            "trust_level": str(self.trust_level),
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "provider_version": self.provider_version,
            "health_status": str(self.health_status),
            "capabilities": [item.qualified_name for item in self.descriptors],
        }

    def supports(self, capability: str, *, version: int | None = None) -> bool:
        base = str(capability or "").split("@", 1)[0]
        for descriptor in self.descriptors:
            if descriptor.name != base:
                continue
            if version is None or int(version) == descriptor.contract_version:
                return True
        return False

    def descriptor(self, capability: str, *, version: int | None = None) -> CapabilityDescriptor | None:
        base = str(capability or "").split("@", 1)[0]
        for descriptor in self.descriptors:
            if descriptor.name != base:
                continue
            if version is None or int(version) == descriptor.contract_version:
                return descriptor
        return None


class CapabilityRegistry:
    """进程内能力注册表（阶段 2 的 Broker 从这里找 Provider）。"""

    def __init__(self, *, catalog: CapabilityCatalog | None = None) -> None:
        self._catalog = catalog or capability_catalog
        # (能力限定名, provider_id) → 注册项；同一能力可有多个 Provider（不同设备）。
        self._by_key: dict[tuple[str, str], ProviderRegistration] = {}

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    # ── 注册 ──────────────────────────────────────────────

    def register(
        self,
        provider: CapabilityProvider,
        *,
        descriptors: tuple[CapabilityDescriptor, ...] | None = None,
        deployment: Deployment | None = None,
        trust_level: TrustLevel = TrustLevel.BUILTIN,
        plugin_id: str = "",
        plugin_version: str = "",
        provider_version: str = "",
        health_status: ProviderHealth = ProviderHealth.UNKNOWN,
        check_catalog: bool = True,
        execution_plane: ExecutionPlane | str | None = None,
        runtime_kind: RuntimeKind | str | None = None,
    ) -> ProviderRegistration:
        """注册一个 Provider；描述符缺省取 Provider 自述。

        ``check_catalog=True``（默认）时强制与内置目录一致：注册方不能放宽本地性、
        副作用或本机确认要求。第三方/客户端 Provider 在阶段 2 走同一入口。

        ``execution_plane`` / ``runtime_kind`` 记录注册方**声明**的执行位置与运行方式：
        ``deployment`` 把两者混在一起（``worker`` 到底在服务端还是客户端说不清），
        新字段是唯一权威表达。
        """
        declared = tuple(descriptors if descriptors is not None else provider.descriptors)
        if not declared:
            raise ValueError(f"Provider {provider.provider_id} 没有声明任何能力")
        site = deployment if deployment is not None else provider.deployment
        if not isinstance(site, Deployment):
            site = Deployment(str(site))
        for descriptor in declared:
            if check_catalog:
                # 目录里没有的能力需要显式放开（阶段 4 的插件能力会走这条路）。
                self._catalog.assert_catalog_consistent(descriptor)
            # 注册期的位置校验：声明的位置必须允许该能力（本地能力不许注册到服务端）。
            if not descriptor.allows_registration(site):
                raise ValueError(
                    f"能力 {descriptor.qualified_name} 的数据本地性 "
                    f"{descriptor.data_locality} 不允许在 {site} 侧注册"
                )
        registration = ProviderRegistration(
            provider=provider,
            provider_id=provider.provider_id,
            deployment=site,
            descriptors=declared,
            trust_level=trust_level,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            provider_version=provider_version,
            health_status=health_status,
            execution_plane=execution_plane,
            runtime_kind=runtime_kind,
        )
        # 先清掉这个 Provider 的旧登记（同一 Provider 重新注册 = 覆盖，不是叠加），
        # 否则改了描述符/位置之后旧能力仍会留在注册表里（幽灵路由）。
        self.unregister(registration.provider_id)
        for descriptor in declared:
            self._by_key[(descriptor.qualified_name, registration.provider_id)] = registration
        return registration

    def unregister(self, provider_id: str) -> int:
        """摘除一个 Provider 的全部能力（租约过期/客户端断开时用）。"""
        target = str(provider_id or "")
        keys = [key for key in self._by_key if key[1] == target]
        for key in keys:
            self._by_key.pop(key, None)
        return len(keys)

    def update_health(self, provider_id: str, status: ProviderHealth) -> int:
        target = str(provider_id or "")
        touched = 0
        for key, registration in self._by_key.items():
            if key[1] == target:
                registration.health_status = status
                touched += 1
        return touched

    # ── 查询 ──────────────────────────────────────────────

    def registrations(
        self,
        capability: str,
        *,
        version: int | None = None,
        deployment: Deployment | None = None,
    ) -> list[ProviderRegistration]:
        """列出能提供该能力、且满足位置要求的 Provider（去重、不做绑定匹配）。"""
        base = str(capability or "").split("@", 1)[0]
        rows: dict[str, ProviderRegistration] = {}
        for registration in self._by_key.values():
            if not registration.supports(base, version=version):
                continue
            if deployment is not None and registration.deployment is not deployment:
                continue
            rows.setdefault(registration.provider_id, registration)
        return list(rows.values())

    def descriptors(self) -> list[CapabilityDescriptor]:
        seen: dict[str, CapabilityDescriptor] = {}
        for registration in self._by_key.values():
            for descriptor in registration.descriptors:
                seen.setdefault(descriptor.qualified_name, descriptor)
        return list(seen.values())

    def providers(self) -> list[ProviderRegistration]:
        seen: dict[str, ProviderRegistration] = {}
        for registration in self._by_key.values():
            seen.setdefault(registration.provider_id, registration)
        return list(seen.values())

    # ── 调用 ──────────────────────────────────────────────

    async def invoke(
        self,
        invocation: CapabilityInvocation,
        *,
        context: AgentExecutionContext,
        provider_id: str = "",
    ) -> CapabilityResult:
        """把调用交给指定 Provider（``provider_id`` 为空时取唯一候选）。

        这里**只做"找得到吗"的检查**：绑定匹配、租约、策略与审批由 Broker 负责；
        找不到 Provider 时返回结构化的 ``CAPABILITY_MISSING`` 而不是抛异常。
        """
        candidates = self.registrations(
            invocation.capability, version=invocation.contract_version
        )
        if provider_id:
            candidates = [item for item in candidates if item.provider_id == provider_id]
        if not candidates:
            return capability_failure(
                "CAPABILITY_MISSING",
                f"没有 Provider 声明能力 {invocation.qualified_capability}",
                capability=invocation.qualified_capability,
                suggested_action="请安装或启用提供该能力的 Provider",
                request_id=invocation.request_id,
                trace_id=invocation.trace_id,
            )
        if len(candidates) > 1 and not provider_id:
            # 多个候选必须由 Broker 先按绑定/租约选定，这里不猜。
            return capability_failure(
                "CAPABILITY_UNAVAILABLE",
                f"能力 {invocation.qualified_capability} 有多个候选 Provider，需要先按会话/设备选定",
                capability=invocation.qualified_capability,
                details={"candidates": [item.provider_id for item in candidates]},
                request_id=invocation.request_id,
                trace_id=invocation.trace_id,
            )
        registration = candidates[0]
        if not descriptor_allows_deployment(registration):
            return capability_failure(
                "DEPLOYMENT_NOT_ALLOWED",
                f"能力 {invocation.qualified_capability} 不允许在 {registration.deployment} 侧执行",
                capability=invocation.qualified_capability,
                provider_id=registration.provider_id,
                request_id=invocation.request_id,
                trace_id=invocation.trace_id,
            )
        return await registration.provider.invoke(invocation, context=context)


def descriptor_allows_deployment(registration: ProviderRegistration) -> bool:
    """注册项的每个描述符都允许在当前部署位置执行。

    纯核在 :func:`lumi_capability.deployment.descriptor_allows_deployment`；
    本函数保留原签名（吃注册项），因为调用点写的是"注册项"——
    取 ``descriptors`` / ``deployment`` 这一步属于应用侧的形状适配。
    """
    return _pkg_descriptor_allows_deployment(registration.descriptors, registration.deployment)


#: 进程内共享注册表（阶段 1 只注册内置 Provider；阶段 2 由租约驱动客户端注册）。
capability_registry = CapabilityRegistry()


__all__ = [
    "CapabilityProvider",
    "CapabilityRegistry",
    "ProviderRegistration",
    "capability_registry",
    "descriptor_allows_deployment",
]
