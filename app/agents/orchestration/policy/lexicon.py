"""Load the bounded intent lexicon used by the application router."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from lumi_orch.policy.lexicon_models import RoutingLexiconDocument
from pydantic import ValidationError

from app.core.config import settings
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


@lru_cache(maxsize=1)
def load_routing_lexicon() -> RoutingLexiconDocument:
    path = Path(settings.AGENT_ROUTING_LEXICON_PATH)
    try:
        return RoutingLexiconDocument.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        monitor_logger.error(
            "路由动作词典加载失败，拒绝启用词典数据",
            event_type="policy_load_failure",
            category="configuration",
            code="ROUTING_LEXICON_LOAD_FAILED",
            context=MonitorContext(component="routing_lexicon"),
            metadata={"path": str(path), "error": str(exc)[:300]},
            exc_info=exc,
        )
        raise RuntimeError(f"routing lexicon load failed: {path}") from exc


def action_markers() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((entry.id, entry.markers) for entry in load_routing_lexicon().actions)


def object_markers() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((entry.id, entry.markers) for entry in load_routing_lexicon().objects)
