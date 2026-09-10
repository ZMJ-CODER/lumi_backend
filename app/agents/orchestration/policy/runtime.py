"""进程启动时加载策略；刻意不支持热更新。"""

from __future__ import annotations

import json

from app.core.config import settings
from lumi_orch.runner import resolve_node_timeout


def node_timeout_seconds(node, configured: int | None = None) -> int:
    """从应用设置解析节点超时，供 Legacy 与 Temporal 共用。"""
    try:
        overrides = json.loads(str(settings.AGENT_NODE_TOOL_TIMEOUTS_JSON or "{}"))
    except (TypeError, ValueError):
        overrides = {}
    resolved = resolve_node_timeout(
        node,
        default_seconds=int(configured or settings.AGENT_NODE_TIMEOUT_SECONDS),
        tool_timeouts=overrides if isinstance(overrides, dict) else {},
    )
    # 节点可声明短问答/长生成提示；仅在显式提供时覆盖通道默认值，
    # 保持旧计划的超时行为可复现。
    params = node.get("params", {}) if isinstance(node, dict) else getattr(node, "params", {}) or {}
    hint = str(params.get("timeout_hint") or "").strip().lower() if isinstance(params, dict) else ""
    if hint in {"short_qa", "short", "fast"}:
        resolved = min(resolved, 20)
    elif hint in {"long_generation", "long", "report"}:
        resolved = max(resolved, 120)
    estimate = params.get("estimated_output_tokens") if isinstance(params, dict) else None
    if estimate is None and isinstance(params, dict):
        estimate = params.get("max_tokens")
    try:
        if estimate is not None and int(estimate) > 0:
            # 保守按 20 token/s 估算，并保留连接/排队缓冲。
            resolved = max(resolved, min(600, 5 + (int(estimate) + 19) // 20 + 10))
    except (TypeError, ValueError):
        pass
    return resolved
