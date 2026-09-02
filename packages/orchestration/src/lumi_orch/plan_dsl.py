"""类型化、与运行时后端无关的静态计划领域语言。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class InputRef(BaseModel):
    """Reference to a sanitized field emitted by an upstream plan step."""

    source_step: str = Field(min_length=1, max_length=160)
    field: str = Field(default="content", min_length=1, max_length=160)


class OutputContract(BaseModel):
    artifact_type: str = Field(min_length=1, max_length=160)
    fields: list[str] = Field(default_factory=list, max_length=32)


class PlanStep(BaseModel):
    """A compiler-consumable action with explicit data and risk contracts."""

    id: str = Field(min_length=1, max_length=160)
    action: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    input_contract: list[InputRef] = Field(default_factory=list, max_length=32)
    output_contract: OutputContract
    risk_level: Literal["read_only", "write", "external_send", "system_command"] = "read_only"
    depends_on: list[str] = Field(default_factory=list, max_length=32)
