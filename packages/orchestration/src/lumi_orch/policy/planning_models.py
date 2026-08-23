"""Bounded data contract for deterministic planning-path classification."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


TemplateName = Literal[
    "invoice_filter_flow",
    "daily_brief_flow",
    "document_analysis_flow",
    "document_compare_flow",
    "document_combine_flow",
    "document_translate_flow",
]


class TemplateMarkerEntry(BaseModel):
    name: TemplateName
    markers: tuple[str, ...] = Field(min_length=1, max_length=48)

    @field_validator("markers")
    @classmethod
    def non_empty_unique_markers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values if value and value.strip())
        if len(cleaned) != len(values) or len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("planning markers must be non-empty and unique per entry")
        return cleaned


class PlanningPolicyDocument(BaseModel):
    """Data only: classifier phrase sets, never graph nodes or tool names."""

    version: Literal[1]
    template_markers: tuple[TemplateMarkerEntry, ...] = Field(min_length=1, max_length=16)
    document_required_templates: tuple[TemplateName, ...] = Field(default_factory=tuple, max_length=16)
    semi_structure_markers: tuple[str, ...] = Field(min_length=1, max_length=64)
    script_markers: tuple[str, ...] = Field(min_length=1, max_length=64)
    multi_topic_markers: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("template_markers")
    @classmethod
    def unique_template_names(cls, values: tuple[TemplateMarkerEntry, ...]) -> tuple[TemplateMarkerEntry, ...]:
        names = [entry.name for entry in values]
        if len(names) != len(set(names)):
            raise ValueError("planning template names must be unique")
        return values

    @field_validator("document_required_templates")
    @classmethod
    def document_templates_must_be_unique(cls, values: tuple[TemplateName, ...]) -> tuple[TemplateName, ...]:
        if len(values) != len(set(values)):
            raise ValueError("document-required template names must be unique")
        return values
