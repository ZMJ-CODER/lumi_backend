"""长期记忆调试接口数据模型（仅 superadmin）."""

from pydantic import BaseModel, Field


class UpdateMemoryRequest(BaseModel):
    content: str | None = Field(default=None, description="事实文本（L1 隐私记忆不允许编辑）")
    memory_type: str | None = Field(default=None, description="identity / preference / experience / goal")
    importance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
