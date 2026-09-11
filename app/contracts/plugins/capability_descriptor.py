"""应用侧能力描述符入口（定义在 ``lumi_contracts.plugins.capability_descriptor``）。"""

from __future__ import annotations

from lumi_contracts.plugins.capability_descriptor import (
    CapabilityDescriptor,
    isolation_floor,
)

__all__ = ["CapabilityDescriptor", "isolation_floor"]
