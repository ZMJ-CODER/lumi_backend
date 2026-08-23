"""Process-start loading for the constrained TCA threshold policy."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from lumi_orch.policy.tca_models import TcaPolicyDocument
from pydantic import ValidationError

from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


DEFAULT_TCA_POLICY = TcaPolicyDocument.model_validate({
    "version": 1,
    "weights": {
        "entity_count": 0.18, "implicitness": 0.20, "dependency": 0.25,
        "ambiguity": 0.25, "history_dependency": 0.12,
    },
    "thresholds": {
        "explicit_workflow_dependency": 0.35, "m2_dependency": 0.35,
        "m3_ambiguity": 0.65, "classifier_confidence": 0.70, "history_dynamic": 0.60,
    },
})


@lru_cache(maxsize=1)
def load_tca_policy() -> TcaPolicyDocument:
    """Load once; an invalid deployment asset falls back to the audited baseline."""
    from app.core.config import settings

    path = Path(str(getattr(settings, "AGENT_TCA_POLICY_PATH", "config/agent_policies/tca_rules.yaml")))
    try:
        return TcaPolicyDocument.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        monitor_logger.error(
            "TCA 策略加载失败，继续使用内置阈值",
            event_type="policy_load_failure",
            category="configuration",
            code="TCA_POLICY_LOAD_FAILED",
            context=MonitorContext(component="tca_policy"),
            metadata={"path": str(path), "error": str(exc)[:300]},
            exc_info=exc,
        )
        return DEFAULT_TCA_POLICY
