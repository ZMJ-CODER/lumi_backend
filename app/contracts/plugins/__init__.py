"""应用侧插件/能力契约入口（阶段 0 冻结）。

**单一事实来源**：形状定义全部在 ``lumi_contracts.plugins``（backend-neutral，内核也
可以 import）。本包只做**应用侧稳定入口**与职责说明，不复制字段定义——两份定义必然
漂移，而漂移的契约比没有契约更危险。

对应关系（方案第二节 1 的目录即本包）::

    app/contracts/plugins/manifest.py               → lumi_contracts.plugins.manifest
    app/contracts/plugins/capability_descriptor.py  → ...capability_descriptor
    app/contracts/plugins/capability_invocation.py  → ...capability_invocation
    app/contracts/plugins/capability_result.py      → ...capability_result
    app/contracts/plugins/provider_lease.py         → ...provider_lease
    app/contracts/plugins/plugin_snapshot.py        → ...plugin_snapshot
    app/contracts/plugins/view_contribution.py      → ...view_contribution

应用层模块（注册/路由/安装/策略）**不得**在本包内实现，各自独立：见
``app/agents/capabilities/``（Registry / Broker）、``app/services/plugins/``（安装与
生命周期）。
"""

from __future__ import annotations

from app.contracts.plugins.capability_descriptor import CapabilityDescriptor
from app.contracts.plugins.capability_invocation import (
    CapabilityInvocation,
    SessionBinding,
)
from app.contracts.plugins.capability_result import (
    INSTALL_REQUIRED_CAPABILITY_ERRORS,
    RETRYABLE_CAPABILITY_ERRORS,
    CapabilityErrorCode,
    CapabilityResult,
    CapabilityUsage,
    capability_failure,
    capability_ok,
)
from app.contracts.plugins.manifest import (
    PluginEntrypoints,
    PluginHealthcheck,
    PluginManifest,
    PluginPermission,
    PluginProvides,
    PluginRequires,
    PluginResourceLimits,
    PluginSignature,
)
from app.contracts.plugins.plugin_snapshot import (
    CapabilityBinding,
    PluginRef,
    PluginSnapshot,
    PolicyRef,
    ProviderRef,
)
from app.contracts.plugins.provider_lease import ProviderLease
from app.contracts.plugins.view_contribution import (
    VIEW_DATA_MAX_BYTES,
    VIEW_TYPES,
    ViewContribution,
)
from app.contracts.plugins.vocabulary import (
    ACTIVATABLE_PLUGIN_KINDS,
    APPROVAL_REQUIRED_SIDE_EFFECTS,
    DEVELOPER_ONLY_PLUGIN_KINDS,
    DataLocality,
    Deployment,
    IsolationLevel,
    PluginKind,
    ProviderHealth,
    SideEffectKind,
    TrustLevel,
    deployment_allows,
    isolation_for_trust,
    parse_data_locality,
    parse_plugin_kind,
)

__all__ = [
    "ACTIVATABLE_PLUGIN_KINDS",
    "APPROVAL_REQUIRED_SIDE_EFFECTS",
    "DEVELOPER_ONLY_PLUGIN_KINDS",
    "INSTALL_REQUIRED_CAPABILITY_ERRORS",
    "RETRYABLE_CAPABILITY_ERRORS",
    "VIEW_DATA_MAX_BYTES",
    "VIEW_TYPES",
    "CapabilityBinding",
    "CapabilityDescriptor",
    "CapabilityErrorCode",
    "CapabilityInvocation",
    "CapabilityResult",
    "CapabilityUsage",
    "DataLocality",
    "Deployment",
    "IsolationLevel",
    "PluginEntrypoints",
    "PluginHealthcheck",
    "PluginKind",
    "PluginManifest",
    "PluginPermission",
    "PluginProvides",
    "PluginRef",
    "PluginRequires",
    "PluginResourceLimits",
    "PluginSignature",
    "PluginSnapshot",
    "PolicyRef",
    "ProviderHealth",
    "ProviderLease",
    "ProviderRef",
    "SessionBinding",
    "SideEffectKind",
    "TrustLevel",
    "ViewContribution",
    "capability_failure",
    "capability_ok",
    "deployment_allows",
    "isolation_for_trust",
    "parse_data_locality",
    "parse_plugin_kind",
]
