"""用户自建 Workflow Skill 的 API 契约。

只接受声明式步骤，明确不接受 Python、Shell、URL 或任意可执行代码。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


_GOALS = {"GENERATE", "RETRIEVE", "ANALYZE", "EXECUTE", "INTERACT"}
_SOURCES = {
    "USER_INPUT", "ATTACHED_FILE", "LOCAL_KNOWLEDGE", "PUBLIC_WEB",
    "EXTERNAL_API", "SYSTEM_STATE",
}


def _normalize_capabilities(values: list[str], allowed: set[str], field: str) -> list[str]:
    normalized = [str(value).strip().upper() for value in values if str(value).strip()]
    invalid = set(normalized) - allowed
    if invalid:
        raise ValueError(f"{field} 包含不支持的能力: {', '.join(sorted(invalid))}")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} 不能重复")
    return normalized


class WorkflowSkillStep(BaseModel):
    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class WorkflowToolDependency(BaseModel):
    """A governed atomic capability required by a Workflow Skill."""

    name: str = Field(min_length=1, max_length=180, pattern=r"^[A-Za-z0-9_.:-]+(?:__[A-Za-z0-9_.:-]+)*$")
    min_version: str = Field(default="0.0.0", pattern=r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
    required: bool = True
    provider: Literal["any", "backend", "sandbox", "desktop_mcp", "external_mcp"] = "any"


class WorkflowDependencies(BaseModel):
    tools: list[WorkflowToolDependency] = Field(default_factory=list, max_length=24)
    providers: list[Literal["backend", "sandbox", "desktop_mcp", "external_mcp"]] = Field(
        default_factory=list, max_length=4
    )
    sources: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("tools")
    @classmethod
    def unique_dependencies(cls, values: list[WorkflowToolDependency]) -> list[WorkflowToolDependency]:
        names = [item.name for item in values]
        if len(names) != len(set(names)):
            raise ValueError("dependencies.tools 不能重复")
        return values


class CreateWorkflowSkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]{0,99}$")
    display_name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="user", min_length=1, max_length=80)
    scenes: list[str] = Field(default_factory=lambda: ["office"], min_length=1, max_length=4)
    allowed_tools: list[str] = Field(default_factory=list, max_length=24)
    steps: list[WorkflowSkillStep] = Field(default_factory=list, max_length=24)
    dependencies: WorkflowDependencies = Field(default_factory=WorkflowDependencies)
    execution_scope: Literal["backend", "client", "backend_orchestrates_client"] = "backend"
    availability_policy: Literal["require_online_client", "allow_server_fallback", "fail_if_missing"] = "fail_if_missing"
    fallback_policy: Literal["fail", "clarify", "direct_answer", "alternate_tool"] = "clarify"
    approval_policy: Literal["none", "before_write", "before_submit"] = "none"
    prompt_body: str = Field(default="", max_length=16000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    provided_goals: list[str] = Field(default_factory=list, max_length=5)
    provided_sources: list[str] = Field(default_factory=list, max_length=6)
    safety_level: str = Field(default="READ_ONLY", pattern=r"^(READ_ONLY|SAFE_WRITE|RISKY_WRITE|CRITICAL)$")

    @field_validator("allowed_tools")
    @classmethod
    def unique_tool_names(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_tools 不能重复")
        return normalized

    @field_validator("provided_goals")
    @classmethod
    def valid_goals(cls, values: list[str]) -> list[str]:
        return _normalize_capabilities(values, _GOALS, "provided_goals")

    @field_validator("provided_sources")
    @classmethod
    def valid_sources(cls, values: list[str]) -> list[str]:
        return _normalize_capabilities(values, _SOURCES, "provided_sources")


class UpdateWorkflowSkillRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    scenes: list[str] | None = Field(default=None, min_length=1, max_length=4)
    allowed_tools: list[str] | None = Field(default=None, max_length=24)
    steps: list[WorkflowSkillStep] | None = Field(default=None, max_length=24)
    dependencies: WorkflowDependencies | None = None
    execution_scope: Literal["backend", "client", "backend_orchestrates_client"] | None = None
    availability_policy: Literal["require_online_client", "allow_server_fallback", "fail_if_missing"] | None = None
    fallback_policy: Literal["fail", "clarify", "direct_answer", "alternate_tool"] | None = None
    approval_policy: Literal["none", "before_write", "before_submit"] | None = None
    prompt_body: str | None = Field(default=None, max_length=16000)
    input_schema: dict[str, Any] | None = None
    provided_goals: list[str] | None = Field(default=None, max_length=5)
    provided_sources: list[str] | None = Field(default=None, max_length=6)
    safety_level: str | None = Field(default=None, pattern=r"^(READ_ONLY|SAFE_WRITE|RISKY_WRITE|CRITICAL)$")
    status: str | None = Field(default=None, pattern=r"^(enabled|disabled)$")

    @field_validator("provided_goals")
    @classmethod
    def valid_update_goals(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else _normalize_capabilities(values, _GOALS, "provided_goals")

    @field_validator("provided_sources")
    @classmethod
    def valid_update_sources(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else _normalize_capabilities(values, _SOURCES, "provided_sources")
