"""适配层：遗留结果 → 当前契约；工具注册与治理校验。"""

from __future__ import annotations

from lumi_contracts.adapters.legacy import (
    LegacyEnvelopeAdapter,
    UnsupportedContractVersion,
    adapt_tool_result,
)
from lumi_contracts.adapters.registry import (
    DEFAULT_TRUSTED_NAMESPACES,
    ToolRegistry,
    default_tool_registry,
    register_tool,
)

__all__ = [
    "DEFAULT_TRUSTED_NAMESPACES",
    "LegacyEnvelopeAdapter",
    "ToolRegistry",
    "UnsupportedContractVersion",
    "adapt_tool_result",
    "default_tool_registry",
    "register_tool",
]
