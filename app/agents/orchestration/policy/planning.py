"""进程启动时加载受限确定性规划词汇。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from lumi_orch.policy.planning_models import PlanningPolicyDocument
from pydantic import ValidationError

from app.core.config import settings
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


@lru_cache(maxsize=1)
def load_planning_policy() -> PlanningPolicyDocument:
    path = Path(settings.AGENT_PLANNING_POLICY_PATH)
    try:
        return PlanningPolicyDocument.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        monitor_logger.error(
            "规划策略加载失败，拒绝启用策略数据",
            event_type="policy_load_failure",
            category="configuration",
            code="PLANNING_POLICY_LOAD_FAILED",
            context=MonitorContext(component="planning_policy"),
            metadata={"path": str(path), "error": str(exc)[:300]},
            exc_info=exc,
        )
        raise RuntimeError(f"planning policy load failed: {path}") from exc


def planning_markers() -> tuple[dict[str, tuple[str, ...]], frozenset[str], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return immutable classifier data; execution semantics remain in intent.py."""
    policy = load_planning_policy()
    return (
        {entry.name: entry.markers for entry in policy.template_markers},
        frozenset(policy.document_required_templates),
        policy.semi_structure_markers,
        policy.script_markers,
        policy.multi_topic_markers,
    )
