"""从 YAML 加载并解析引擎管理的执行默认策略。"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from lumi_orch.job_spec import NodeExecutionSpec
from lumi_orch.policy.execution_models import ExecutionDefaultsDocument
from pydantic import ValidationError

from app.core.config import settings


@lru_cache(maxsize=1)
def load_execution_defaults() -> ExecutionDefaultsDocument:
    path = Path(settings.AGENT_EXECUTION_DEFAULTS_PATH)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ExecutionDefaultsDocument.model_validate(payload)
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        raise RuntimeError(f"execution defaults load failed: {path}") from exc


def resolve_node_execution_spec(
    node_spec: NodeExecutionSpec,
    *,
    task_policy: dict[str, Any] | None = None,
) -> tuple[NodeExecutionSpec, dict[str, Any]]:
    """Merge defaults < task policy < node facts and return an auditable snapshot."""
    document = load_execution_defaults()
    defaults = document.defaults[node_spec.resource_class]
    base = {
        "resource_class": node_spec.resource_class,
        "side_effect": node_spec.side_effect,
        "idempotency": node_spec.idempotency.model_dump(mode="json"),
        "retry": defaults.retry.model_dump(mode="json"),
        "timeout_seconds": defaults.timeout_seconds,
        "fallback": node_spec.fallback,
        "failure_isolation": node_spec.failure_isolation,
        "critical": node_spec.critical,
    }
    if task_policy:
        for key in ("retry", "timeout_seconds", "fallback", "failure_isolation", "critical"):
            if key in task_policy:
                if key == "retry" and isinstance(task_policy[key], dict):
                    base[key] = {**base[key], **task_policy[key]}
                else:
                    base[key] = task_policy[key]
    node_data = node_spec.model_dump(mode="json")
    for key in ("retry", "timeout_seconds", "fallback", "failure_isolation", "critical"):
        value = node_data.get(key)
        if key == "retry" and isinstance(value, dict):
            # Node defaults are intentionally treated as unset when they equal
            # the model default; explicit values still override task policy.
            if value != NodeExecutionSpec().retry.model_dump(mode="json"):
                base[key] = {**base[key], **value}
        elif value is not None and value != getattr(NodeExecutionSpec(), key):
            base[key] = value
    resolved = NodeExecutionSpec.model_validate(base)
    raw = json.dumps(resolved.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    policy_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return resolved, {"version": document.version, "sha256": policy_hash, "resolved": resolved.model_dump(mode="json")}


def channel_concurrency(channel: str) -> int:
    return max(1, int(load_execution_defaults().concurrency.get(channel, 1)))
