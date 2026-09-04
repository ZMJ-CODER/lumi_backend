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


_CHANNEL_TIMEOUT_SETTINGS = {
    "direct_llm": "AGENT_NODE_TIMEOUT_DIRECT_LLM_SECONDS",
    "deterministic_script": "AGENT_NODE_TIMEOUT_SCRIPT_SECONDS",
    "rag": "AGENT_NODE_TIMEOUT_RAG_SECONDS",
    "agent": "AGENT_NODE_TIMEOUT_AGENT_SECONDS",
}


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
    node_params: dict[str, Any] | None = None,
    node_metadata: dict[str, Any] | None = None,
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
    # Explicit node/task policy values remain authoritative.  Dynamic
    # estimation only fills an unset timeout; it must not silently replace a
    # business-declared safety window (for example a 30s bounded API call).
    timeout_is_explicit = node_spec.timeout_seconds is not None or bool(
        task_policy and task_policy.get("timeout_seconds") is not None
    )
    if node_params and not timeout_is_explicit:
        from lumi_orch.runner import resolve_node_timeout

        try:
            tool_overrides = json.loads(str(settings.AGENT_NODE_TOOL_TIMEOUTS_JSON or "{}"))
        except (TypeError, ValueError):
            tool_overrides = {}
        dynamic_timeout = resolve_node_timeout(
            {"params": node_params, "metadata": node_metadata or {}},
            default_seconds=int(resolved.timeout_seconds or defaults.timeout_seconds),
            channel_timeouts={
                channel: int(getattr(settings, setting_name, 0) or 0)
                for channel, setting_name in _CHANNEL_TIMEOUT_SETTINGS.items()
            },
            tool_timeouts=tool_overrides if isinstance(tool_overrides, dict) else {},
        )
        resolved = resolved.model_copy(update={"timeout_seconds": dynamic_timeout})
    raw = json.dumps(resolved.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    policy_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return resolved, {"version": document.version, "sha256": policy_hash, "resolved": resolved.model_dump(mode="json")}


def channel_concurrency(channel: str) -> int:
    return max(1, int(load_execution_defaults().concurrency.get(channel, 1)))
