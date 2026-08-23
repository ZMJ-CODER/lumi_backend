"""Pydantic contract for the intentionally small policy language."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


ConditionOperator = Literal["eq", "gte", "lte", "contains"]
RouteChannelName = Literal["direct_llm", "deterministic_script", "rag", "agent"]


class PolicyCondition(BaseModel):
    feature: str = Field(min_length=1, max_length=80)
    op: ConditionOperator
    value: Any


class RoutingPolicyRule(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    priority: int = Field(ge=0, le=10_000)
    when: tuple[PolicyCondition, ...] = Field(min_length=1, max_length=12)
    channel: RouteChannelName
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    hooks: tuple[str, ...] = Field(default_factory=tuple, max_length=4)
    overrides: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @field_validator("hooks", "overrides")
    @classmethod
    def unique_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("hook and override names must be unique")
        return values


class RoutingPolicyDocument(BaseModel):
    version: Literal[1]
    rules: tuple[RoutingPolicyRule, ...] = Field(default_factory=tuple, max_length=64)

    @field_validator("rules")
    @classmethod
    def unique_rules(cls, rules: tuple[RoutingPolicyRule, ...]) -> tuple[RoutingPolicyRule, ...]:
        ids = [rule.id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("routing policy rule ids must be unique")
        priorities = [rule.priority for rule in rules]
        if len(priorities) != len(set(priorities)):
            raise ValueError("routing policy priorities must be explicit and unique")
        return rules
