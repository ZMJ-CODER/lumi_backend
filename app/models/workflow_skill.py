"""用户自建 Workflow Skill 的 API 契约。

只接受声明式步骤，明确不接受 Python、Shell、URL 或任意可执行代码。
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class WorkflowSkillStep(BaseModel):
    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class CreateWorkflowSkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]{0,99}$")
    display_name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="user", min_length=1, max_length=80)
    scenes: list[str] = Field(default_factory=lambda: ["office"], min_length=1, max_length=4)
    allowed_tools: list[str] = Field(min_length=1, max_length=12)
    steps: list[WorkflowSkillStep] = Field(min_length=1, max_length=16)
    input_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_tools")
    @classmethod
    def unique_tool_names(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_tools 不能重复")
        return normalized


class UpdateWorkflowSkillRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    scenes: list[str] | None = Field(default=None, min_length=1, max_length=4)
    allowed_tools: list[str] | None = Field(default=None, min_length=1, max_length=12)
    steps: list[WorkflowSkillStep] | None = Field(default=None, min_length=1, max_length=16)
    input_schema: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern=r"^(enabled|disabled)$")
