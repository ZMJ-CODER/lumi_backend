"""业务层自有路由词典的受限数据契约。

The kernel validates shape and bounded vocabulary only. It deliberately does
not interpret words or language: matching a phrase to an action remains an
application concern.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


RouteActionName = Literal[
    "lookup_history", "converse", "send", "execute", "modify", "transform",
    "create", "analyze", "read", "query",
]
RouteObjectName = Literal[
    "task_history", "external_resource", "application", "message", "document",
    "data", "task_result", "project",
]
RiskLevel = Literal["read_only", "write", "external_send", "system_command"]


class ActionLexiconEntry(BaseModel):
    model_config = {"extra": "forbid"}
    id: RouteActionName
    markers: tuple[str, ...] = Field(min_length=1, max_length=96)
    risk_level: RiskLevel

    @field_validator("markers")
    @classmethod
    def normalized_unique_markers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values if value and value.strip())
        if len(cleaned) != len(values) or len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("routing lexicon markers must be non-empty and unique per entry")
        return cleaned


class ObjectLexiconEntry(BaseModel):
    model_config = {"extra": "forbid"}
    id: RouteObjectName
    markers: tuple[str, ...] = Field(min_length=1, max_length=96)

    @field_validator("markers")
    @classmethod
    def normalized_unique_markers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values if value and value.strip())
        if len(cleaned) != len(values) or len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("routing lexicon markers must be non-empty and unique per entry")
        return cleaned


class RoutingLexiconDocument(BaseModel):
    model_config = {"extra": "forbid"}
    version: Literal[1]
    actions: tuple[ActionLexiconEntry, ...] = Field(min_length=1, max_length=32)
    objects: tuple[ObjectLexiconEntry, ...] = Field(min_length=1, max_length=32)
    intent_patterns: "IntentPatternDocument" = Field(default_factory=lambda: IntentPatternDocument())

    @field_validator("actions", "objects")
    @classmethod
    def unique_entry_ids(cls, entries: tuple[ActionLexiconEntry | ObjectLexiconEntry, ...]):
        ids = [entry.id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("routing lexicon entry ids must be unique")
        return entries


IntentPatternName = Literal[
    "network", "retrieval", "multiple_connectors", "vague_referents",
    "vague_actions", "bare_query_commands", "greetings", "feedback",
    "implicit_history", "dynamic", "conditional",
]


class IntentPatternDocument(BaseModel):
    """Bounded marker groups consumed by the deterministic router."""

    model_config = {"extra": "forbid"}
    network: dict[str, tuple[str, ...]] = Field(default_factory=lambda: {"explicit": (), "context": ()})
    retrieval: tuple[str, ...] = ()
    multiple_connectors: tuple[str, ...] = ()
    vague_referents: tuple[str, ...] = ()
    vague_actions: tuple[str, ...] = ()
    bare_query_commands: tuple[str, ...] = ()
    greetings: tuple[str, ...] = ()
    feedback: tuple[str, ...] = ()
    implicit_history: tuple[str, ...] = ()
    dynamic: tuple[str, ...] = ()
    conditional: tuple[str, ...] = ()

    @field_validator("network")
    @classmethod
    def validate_network_groups(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        if set(value) - {"explicit", "context"}:
            raise ValueError("network intent patterns only allow explicit/context groups")
        return value

    @field_validator(
        "retrieval", "multiple_connectors", "vague_referents", "vague_actions",
        "bare_query_commands", "greetings", "feedback", "implicit_history",
        "dynamic", "conditional", mode="before",
    )
    @classmethod
    def normalize_markers(cls, values: object) -> tuple[str, ...]:
        items = tuple(str(value).strip() for value in (values or ()) if str(value).strip())
        if len(items) != len(set(item.casefold() for item in items)):
            raise ValueError("intent pattern markers must be unique")
        return items
