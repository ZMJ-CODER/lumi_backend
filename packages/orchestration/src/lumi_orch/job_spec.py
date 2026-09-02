"""编排提交使用的运行时后端无关不可变契约。

The application ``Job`` remains the mutable persistence and presentation
model.  These specs are the frozen execution contract passed to a backend;
neither a router nor a Workflow needs to know the concrete scheduler.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from lumi_orch.resources import ResourceClaim


ResourceClass = Literal["io_bound", "cpu_bound", "external_dependency"]
SideEffect = Literal["pure_read", "write_once", "external_call"]
IdempotencyType = Literal["natural_key", "explicit_key", "non_idempotent"]
RetryError = Literal[
    "timeout", "rate_limit", "transient_error", "capacity", "external_error",
    "review_rejected", "unknown",
]


class RetrySpec(BaseModel):
    """Declarative retry facts interpreted by the execution engine."""

    on: tuple[RetryError, ...] = ("timeout", "rate_limit", "transient_error")
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    backoff: Literal["fixed", "exponential_jitter"] = "exponential_jitter"
    budget_seconds: int | None = Field(default=None, ge=1, le=86400)


class IdempotencySpec(BaseModel):
    type: IdempotencyType = "non_idempotent"
    key_template: str | None = Field(default=None, max_length=512)

    @field_validator("key_template")
    @classmethod
    def require_key_for_keyed_types(cls, value: str | None, info):
        kind = info.data.get("type")
        if kind in {"natural_key", "explicit_key"} and not (value or "").strip():
            raise ValueError("natural_key/explicit_key requires key_template")
        return value


class NodeExecutionSpec(BaseModel):
    """Business-supplied facts; scheduling policy remains engine-owned."""

    resource_class: ResourceClass = "io_bound"
    side_effect: SideEffect = "pure_read"
    idempotency: IdempotencySpec = Field(default_factory=IdempotencySpec)
    retry: RetrySpec = Field(default_factory=RetrySpec)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    fallback: str | None = Field(default=None, max_length=160)
    failure_isolation: bool = False
    critical: bool = False

    @field_validator("critical")
    @classmethod
    def critical_is_not_isolated(cls, value: bool, info) -> bool:
        if value and info.data.get("failure_isolation") is True:
            raise ValueError("critical node cannot also enable failure_isolation")
        return value

    @model_validator(mode="after")
    def validate_side_effect_contract(self) -> "NodeExecutionSpec":
        if self.side_effect == "write_once" and self.idempotency.type == "non_idempotent":
            raise ValueError("write_once requires an explicit or natural idempotency key")
        if self.side_effect == "write_once" and not self.idempotency.key_template:
            raise ValueError("write_once requires idempotency.key_template")
        return self

    def requires_effect_journal(self) -> bool:
        return self.side_effect != "pure_read"


class NodeSpec(BaseModel):
    """One immutable, executable node selected by the planner."""

    id: str = Field(min_length=1, max_length=160)
    agent: str = Field(min_length=1, max_length=120)
    name: str = Field(default="", max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    resource_claims: tuple[ResourceClaim, ...] = ()
    idempotency_key: str | None = Field(default=None, max_length=256)
    approval: bool = False
    approval_note: str = Field(default="", max_length=2000)
    max_retries: int = Field(default=0, ge=0, le=20)
    execution: NodeExecutionSpec = Field(default_factory=NodeExecutionSpec)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _dedupe_dependencies(cls, value: Any) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(item) for item in (value or []) if str(item)))


class NodeResult(BaseModel):
    """Backend-neutral terminal result of one node."""

    node_id: str = Field(min_length=1, max_length=160)
    status: Literal["completed", "failed", "skipped", "interrupted", "escalated"]
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=4000)
    error_code: str | None = Field(default=None, max_length=160)
    retries: int = Field(default=0, ge=0)
    effect_status: str | None = Field(default=None, max_length=40)


class JobSpec(BaseModel):
    """Frozen Job input shared by Legacy and Temporal backends."""

    version: int = Field(default=1, ge=1)
    job_id: str = Field(min_length=1, max_length=160)
    user_id: str = Field(min_length=1, max_length=160)
    user_role: str = Field(default="user", max_length=80)
    scene: str = Field(default="office", max_length=80)
    request: str = Field(default="", max_length=20000)
    nodes: tuple[NodeSpec, ...] = ()
    routing: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = Field(default="", max_length=64)

    def with_fingerprint(self) -> "JobSpec":
        """Return a copy whose digest covers all execution-relevant fields."""
        payload = self.model_dump(exclude={"fingerprint"}, mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return self.model_copy(update={"fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest()})


class JobSnapshot(BaseModel):
    """Backend-independent state returned by query/control operations."""

    job_id: str = Field(min_length=1, max_length=160)
    status: str = Field(min_length=1, max_length=80)
    node_results: tuple[NodeResult, ...] = ()
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=4000)
