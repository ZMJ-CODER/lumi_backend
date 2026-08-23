"""Process-start policy loading; hot reload is deliberately not supported."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.agents.orchestration.policy.engine import PolicyLoadError, RoutingPolicyEngine
from app.core.config import settings
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


def routing_policy_mode() -> str:
    mode = str(settings.AGENT_ROUTING_POLICY_MODE or "shadow").strip().lower()
    if mode in {"legacy", "shadow", "enforce"}:
        return mode
    monitor_logger.error(
        "路由策略模式无效，已禁用策略执行",
        event_type="policy_load_failure",
        category="configuration",
        code="ROUTING_POLICY_INVALID_MODE",
        context=MonitorContext(component="routing_policy"),
        metadata={"mode": mode[:40]},
    )
    return "legacy"


@lru_cache(maxsize=1)
def load_routing_policy() -> RoutingPolicyEngine | None:
    """Load exactly once per process so every decision uses one policy version."""
    if routing_policy_mode() == "legacy":
        return None
    path = Path(settings.AGENT_ROUTING_POLICY_PATH)
    try:
        policy = RoutingPolicyEngine.from_path(path)
    except PolicyLoadError as exc:
        # A process has no previous in-memory policy during startup.  Keeping
        # legacy routing is the safe equivalent; release orchestration emits
        # this event and never silently treats malformed YAML as a new policy.
        monitor_logger.error(
            "路由策略加载失败，继续使用旧路由",
            event_type="policy_load_failure",
            category="configuration",
            code="ROUTING_POLICY_LOAD_FAILED",
            context=MonitorContext(component="routing_policy"),
            metadata={"path": str(path), "error": str(exc)[:300]},
            exc_info=exc,
        )
        return None
    monitor_logger.info(
        "路由策略已加载",
        event_type="policy_loaded",
        category="configuration",
        code="ROUTING_POLICY_LOADED",
        context=MonitorContext(component="routing_policy"),
        metadata={"version": policy.version, "sha256": policy.policy_sha256, "path": str(path)},
    )
    return policy
