"""Constrained data contract for business-owned routing lexicons.

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
    version: Literal[1]
    actions: tuple[ActionLexiconEntry, ...] = Field(min_length=1, max_length=32)
    objects: tuple[ObjectLexiconEntry, ...] = Field(min_length=1, max_length=32)

    @field_validator("actions", "objects")
    @classmethod
    def unique_entry_ids(cls, entries: tuple[ActionLexiconEntry | ObjectLexiconEntry, ...]):
        ids = [entry.id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("routing lexicon entry ids must be unique")
        return entries
