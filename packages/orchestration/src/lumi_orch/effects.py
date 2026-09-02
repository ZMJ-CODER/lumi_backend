"""持久化外部副作用日志的纯状态转换。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field


EffectStatus = Literal["intent", "confirmed", "uncertain"]


class EffectRecord(BaseModel):
    """A body-free audit record for one externally visible operation."""

    status: EffectStatus
    intent: dict[str, Any] = Field(default_factory=dict)
    intent_at: float | None = None
    confirmed_at: float | None = None
    uncertain_at: float | None = None
    reason: str | None = None
    result: dict[str, Any] | None = None
    updated_at: float


def effect_intent_for_node(*, job_id: str, node: Any, tool: str = "") -> dict[str, str]:
    """Produce the stable, body-free intent fingerprint for a task node."""
    params = getattr(node, "params", {}) or {}
    encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "job_id": str(job_id or ""),
        "node_id": str(getattr(node, "id", "") or ""),
        "agent": str(getattr(node, "agent", "") or ""),
        "tool": str(tool or params.get("preferred_tool") or getattr(node, "agent", ""))[:160],
        "params_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def intent_record(intent: Mapping[str, Any] | None = None, *, now: float | None = None) -> EffectRecord:
    timestamp = time.time() if now is None else now
    return EffectRecord(status="intent", intent=dict(intent or {}), intent_at=timestamp, updated_at=timestamp)


def confirm_record(
    previous: Mapping[str, Any] | EffectRecord | None,
    result: dict[str, Any] | None = None,
    *,
    now: float | None = None,
) -> EffectRecord:
    timestamp = time.time() if now is None else now
    prior = _coerce_record(previous)
    return EffectRecord(
        status="confirmed",
        intent=dict(prior.intent),
        intent_at=prior.intent_at,
        confirmed_at=timestamp,
        result=result,
        updated_at=timestamp,
    )


def uncertain_record(
    previous: Mapping[str, Any] | EffectRecord | None,
    reason: str = "execution_interrupted",
    *,
    now: float | None = None,
) -> EffectRecord:
    timestamp = time.time() if now is None else now
    prior = _coerce_record(previous)
    return EffectRecord(
        status="uncertain",
        intent=dict(prior.intent),
        intent_at=prior.intent_at,
        uncertain_at=timestamp,
        reason=str(reason or "execution_interrupted")[:160],
        updated_at=timestamp,
    )


def _coerce_record(previous: Mapping[str, Any] | EffectRecord | None) -> EffectRecord:
    if isinstance(previous, EffectRecord):
        return previous
    if previous:
        return EffectRecord.model_validate(previous)
    return EffectRecord(status="intent", updated_at=0)
