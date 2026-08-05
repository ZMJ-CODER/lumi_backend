"""记忆模块数据模型."""

from pydantic import BaseModel, Field


class UpdateMemoryRequest(BaseModel):
    content: str | None = None
    tags: list[str] | None = None


class MemorySettingsRequest(BaseModel):
    auto_expire_days: int = Field(default=90, ge=1)
    cleanup_threshold: float = Field(default=0.3, ge=0, le=1)
