"""应用侧 ``PluginManifest`` 入口（定义在 ``lumi_contracts.plugins.manifest``）。"""

from __future__ import annotations

from lumi_contracts.plugins.manifest import (
    CAPABILITY_NAME_RE,
    PLUGIN_ID_RE,
    PluginEntrypoints,
    PluginHealthcheck,
    PluginManifest,
    PluginPermission,
    PluginProvides,
    PluginRequires,
    PluginResourceLimits,
    PluginSignature,
)

__all__ = [
    "CAPABILITY_NAME_RE",
    "PLUGIN_ID_RE",
    "PluginEntrypoints",
    "PluginHealthcheck",
    "PluginManifest",
    "PluginPermission",
    "PluginProvides",
    "PluginRequires",
    "PluginResourceLimits",
    "PluginSignature",
]
