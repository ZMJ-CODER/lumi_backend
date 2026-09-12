"""``PluginSnapshot`` / ``CapabilitySnapshot`` / ``PolicySnapshot``（阶段 0 冻结）。

为什么必须进 Job 快照：刷新、重试、审计、以及"为什么这次任务行为和上次不一样"的
排查，都需要知道**当时实际用了什么版本**。只记录"当前安装了什么"是不够的——
任务可能跑在升级前，回滚后也还要能解释历史。

三个快照各自回答一个问题：

* ``plugin_snapshot``：这次任务用了哪些插件（含客户端 Provider）与版本；
* ``capability_snapshot``：每个能力最终由**哪个 Provider / 哪台设备 / 哪个契约版本**
  承担（含服务端或客户端）；
* ``policy_snapshot``：这次任务的策略包与版本（决定"允许做到什么程度"）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lumi_contracts.plugins.capability_descriptor import CapabilityDescriptor
from lumi_contracts.plugins.provider_lease import ProviderLease


class PluginRef(BaseModel):
    """快照里的单个插件引用（只放恢复/审计需要的字段）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    kind: str = ""
    deployment: str = ""
    digest: str = ""
    trust_level: str = ""


class ProviderRef(BaseModel):
    """快照里参与过调用的 Provider。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str = ""
    deployment: str = ""
    #: 实际执行位置与运行方式（来自租约；不是 Manifest 的声明值）。
    execution_plane: str = ""
    runtime_kind: str = ""
    executor_type: str = ""
    plugin_id: str = ""
    device_id: str = ""
    #: 快照时刻的健康状态（"当时它是健康的"是可审计事实，与"现在"无关）。
    health_status: str = ""


class CapabilityBinding(BaseModel):
    """一个能力最终绑定到谁（按会话/工作区/设备维度）。"""

    model_config = ConfigDict(extra="forbid")

    capability: str
    provider_id: str = ""
    deployment: str = ""
    #: 这次绑定实际在哪一侧、以什么运行方式执行。
    execution_plane: str = ""
    runtime_kind: str = ""
    executor_type: str = ""
    device_id: str = ""
    workspace_id: str = ""
    contract_version: int = 1
    data_locality: str = ""
    #: 是否由策略允许的位置切换而来（hybrid 场景审计用）。
    routed_by_policy: bool = False


class PolicyRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str = ""
    source: str = "builtin"


class PluginSnapshot(BaseModel):
    """任务使用的插件/Provider/策略/能力绑定快照。"""

    model_config = ConfigDict(extra="forbid")

    skills: list[PluginRef] = Field(default_factory=list)
    providers: list[ProviderRef] = Field(default_factory=list)
    policies: list[PolicyRef] = Field(default_factory=list)
    capabilities: list[CapabilityBinding] = Field(default_factory=list)

    def to_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    # ── 构造入口（调用方不必手拼字段）────────────────────

    @classmethod
    def from_leases(
        cls,
        *,
        leases: list[ProviderLease] | None = None,
        capabilities: list[CapabilityDescriptor] | None = None,
        policies: list[PolicyRef] | None = None,
        skills: list[PluginRef] | None = None,
    ) -> "PluginSnapshot":
        """由租约 + 描述符构造能力绑定快照（同一 (能力, Provider) 只留一条）。"""
        bindings: list[CapabilityBinding] = []
        seen: set[str] = set()
        for lease in leases or []:
            key = f"{lease.qualified_capability}|{lease.provider_id}|{lease.device_id}"
            if key in seen:
                continue
            seen.add(key)
            bindings.append(
                CapabilityBinding(
                    capability=lease.qualified_capability,
                    provider_id=lease.provider_id,
                    deployment=str(lease.deployment),
                    # 实际值（租约推导），刷新后仍能回答"当时谁在执行"。
                    execution_plane=str(lease.plane()),
                    runtime_kind=str(lease.runtime()),
                    executor_type=lease.executor_type(),
                    device_id=lease.device_id,
                    workspace_id=lease.workspace_id,
                    contract_version=int(lease.contract_version),
                )
            )
        by_name = {item.qualified_name: item for item in capabilities or []}
        for binding in bindings:
            descriptor = by_name.get(binding.capability)
            if descriptor is not None:
                binding.data_locality = str(descriptor.data_locality)
        providers: list[ProviderRef] = []
        seen_providers: set[str] = set()
        for lease in leases or []:
            if lease.provider_id in seen_providers:
                continue
            seen_providers.add(lease.provider_id)
            providers.append(
                ProviderRef(
                    id=lease.provider_id,
                    version=lease.provider_version,
                    deployment=str(lease.deployment),
                    execution_plane=str(lease.plane()),
                    runtime_kind=str(lease.runtime()),
                    executor_type=lease.executor_type(),
                    plugin_id=lease.plugin_id,
                    device_id=lease.device_id,
                    health_status=str(lease.health_status),
                )
            )
        return cls(
            skills=list(skills or []),
            providers=providers,
            policies=list(policies or []),
            capabilities=bindings,
        )

    def capability_snapshot(self) -> list[dict[str, Any]]:
        """``run_view.capability_snapshot`` 的形状（前端与审计直接消费）。"""
        return [item.model_dump(mode="json", exclude_none=True) for item in self.capabilities]

    def policy_snapshot(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json", exclude_none=True) for item in self.policies]


__all__ = [
    "CapabilityBinding",
    "PluginRef",
    "PluginSnapshot",
    "PolicyRef",
    "ProviderRef",
]
