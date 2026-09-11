"""应用侧插件词表入口（定义在 ``lumi_contracts.plugins.vocabulary``）。"""

from __future__ import annotations

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
    "CapabilityStatus",
    "DEVELOPER_ONLY_PLUGIN_KINDS",
    "DataLocality",
    "Deployment",
    "IsolationLevel",
    "PluginKind",
    "ProviderHealth",
    "SideEffectKind",
    "TrustLevel",
    "deployment_allows",
    "isolation_for_trust",
    "parse_data_locality",
    "parse_plugin_kind",
]
