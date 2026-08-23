"""Deterministic per-node hard-timeout policy shared by both runtimes."""

from __future__ import annotations

import json

from lumi_orch.runner import resolve_node_timeout

from app.core.config import settings


_CHANNEL_SETTINGS = {
    "direct_llm": "AGENT_NODE_TIMEOUT_DIRECT_LLM_SECONDS",
    "deterministic_script": "AGENT_NODE_TIMEOUT_SCRIPT_SECONDS",
    "rag": "AGENT_NODE_TIMEOUT_RAG_SECONDS",
    "agent": "AGENT_NODE_TIMEOUT_AGENT_SECONDS",
}


def node_timeout_seconds(node, configured: int | None = None) -> int:
    """Return a bounded timeout; a preferred-tool override wins over channel."""
    try:
        overrides = json.loads(str(settings.AGENT_NODE_TOOL_TIMEOUTS_JSON or "{}"))
    except (TypeError, ValueError):
        overrides = {}
    return resolve_node_timeout(
        node,
        default_seconds=int(configured or settings.AGENT_NODE_TIMEOUT_SECONDS),
        channel_timeouts={
            channel: int(getattr(settings, setting_name, 0) or 0)
            for channel, setting_name in _CHANNEL_SETTINGS.items()
        },
        tool_timeouts=overrides if isinstance(overrides, dict) else {},
    )
