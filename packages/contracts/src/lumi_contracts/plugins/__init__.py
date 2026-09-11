"""插件/能力契约包（``lumi_contracts.plugins``）。

模块地图（阶段 0 冻结，后端与执行内核同源，**不依赖 ``app``**）::

    vocabulary              插件类型 / 部署位置 / 数据本地性 / 信任 / 副作用的严格语义
    manifest                PluginManifest：插件自述（安装与激活的唯一事实来源）
    capability_descriptor   CapabilityDescriptor：能力"能做什么" + 输入/输出 Schema
    capability_invocation   CapabilityInvocation：一次调用的请求形状（含会话绑定）
    capability_result       CapabilityResult：统一结果 + 稳定能力错误码
    provider_lease          ProviderLease：注册/心跳/过期即摘除的存活凭证
    plugin_snapshot         Plugin/Capability/Policy 快照（进 Job.run_view）
    view_contribution       声明式视图贡献（不允许注入 JSX/JS）

设计边界：本包只定义**形状与语义**，不做注册、路由、加载、安装——那些是 ``app``
层的职责（``PluginRegistry`` / ``CapabilityRegistry`` / ``CapabilityBroker`` /
``PluginInstaller``）。内核可以 import 本包，但**不得** import ``app.*``。
"""

from __future__ import annotations

from lumi_contracts.plugins.capability_descriptor import CapabilityDescriptor
from lumi_contracts.plugins.capability_invocation import (
    CapabilityInvocation,
    SessionBinding,
)
from lumi_contracts.plugins.capability_result import (
    ERROR_TO_CAPABILITY_STATUS,
    INSTALL_REQUIRED_CAPABILITY_ERRORS,
    RETRYABLE_CAPABILITY_ERRORS,
    CapabilityErrorCode,
    CapabilityResult,
    CapabilityUsage,
    capability_failure,
    capability_ok,
    capability_status_for,
)
from lumi_contracts.plugins.manifest import (
    PluginEntrypoints,
    PluginHealthcheck,
    PluginManifest,
    PluginPermission,
    PluginProvides,
    PluginRequires,
    PluginResourceLimits,
    PluginSignature,
)
from lumi_contracts.plugins.plugin_snapshot import (
    CapabilityBinding,
    PluginRef,
    PluginSnapshot,
    PolicyRef,
    ProviderRef,
)
from lumi_contracts.plugins.provider_lease import ProviderLease
from lumi_contracts.plugins.view_contribution import (
    VIEW_DATA_MAX_BYTES,
    VIEW_TYPES,
    ViewContribution,
)
from lumi_contracts.plugins.vocabulary import (
    ACTIVATABLE_PLUGIN_KINDS,
    APPROVAL_REQUIRED_SIDE_EFFECTS,
    DEVELOPER_ONLY_PLUGIN_KINDS,
    CapabilityStatus,
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
    "CapabilityBinding",
    "CapabilityDescriptor",
    "CapabilityErrorCode",
    "CapabilityInvocation",
    "CapabilityResult",
    "CapabilityStatus",
    "CapabilityUsage",
    "DEVELOPER_ONLY_PLUGIN_KINDS",
    "DataLocality",
    "Deployment",
    "ERROR_TO_CAPABILITY_STATUS",
    "INSTALL_REQUIRED_CAPABILITY_ERRORS",
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
    "RETRYABLE_CAPABILITY_ERRORS",
    "SessionBinding",
    "SideEffectKind",
    "TrustLevel",
    "VIEW_DATA_MAX_BYTES",
    "VIEW_TYPES",
    "ViewContribution",
    "capability_failure",
    "capability_ok",
    "capability_status_for",
    "deployment_allows",
    "isolation_for_trust",
    "parse_data_locality",
    "parse_plugin_kind",
]
