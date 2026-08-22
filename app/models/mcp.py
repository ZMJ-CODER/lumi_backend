"""用户级外部 MCP 工具绑定 API 模型。"""

from pydantic import BaseModel, Field, field_validator


class McpToolBindingCreate(BaseModel):
    server_name: str = Field(min_length=1, max_length=100)
    raw_tool_name: str = Field(min_length=1, max_length=200)
    domain: str = Field(default="external", max_length=80)
    intent_tags: list[str] = Field(default_factory=list, max_length=20)
    scenes: list[str] = Field(default_factory=lambda: ["office"], max_length=8)
    permission: str = "user"
    write_op: bool | None = None
    requires_confirmation: bool | None = None
    confirmation_mode: str | None = None
    idempotent: bool | None = None

    @field_validator("scenes")
    @classmethod
    def office_only_until_user_bound_transport_exists(cls, value: list[str]) -> list[str]:
        allowed = {"office"}
        result = sorted({str(item).strip() for item in value if str(item).strip()})
        if not result or not set(result).issubset(allowed):
            raise ValueError("外部 MCP 工具当前只允许绑定到 office 场景")
        return result

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, value: str) -> str:
        if value not in {"user", "admin"}:
            raise ValueError("permission 必须是 user 或 admin")
        return value


class McpToolBindingUpdate(BaseModel):
    enabled: bool


class McpToolBindingReview(BaseModel):
    approved: bool
