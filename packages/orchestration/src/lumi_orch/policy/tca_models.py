"""Constrained data contract for task-complexity policy tuning."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class TcaWeights(BaseModel):
    entity_count: float = Field(ge=0, le=1)
    implicitness: float = Field(ge=0, le=1)
    dependency: float = Field(ge=0, le=1)
    ambiguity: float = Field(ge=0, le=1)
    history_dependency: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def sums_to_one(self) -> "TcaWeights":
        if abs(sum(self.model_dump().values()) - 1.0) > 0.001:
            raise ValueError("TCA weights must sum to 1.0")
        return self


class TcaThresholds(BaseModel):
    explicit_workflow_dependency: float = Field(ge=0, le=1)
    m2_dependency: float = Field(ge=0, le=1)
    m3_ambiguity: float = Field(ge=0, le=1)
    classifier_confidence: float = Field(ge=0, le=1)
    history_dynamic: float = Field(ge=0, le=1)


class TcaPolicyDocument(BaseModel):
    version: Literal[1]
    weights: TcaWeights
    thresholds: TcaThresholds
