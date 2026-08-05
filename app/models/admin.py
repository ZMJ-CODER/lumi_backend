"""管理员模块数据模型."""

from pydantic import BaseModel, Field


class UpdateUserRequest(BaseModel):
    role: str | None = None
    status: str | None = None  # active / disabled


class RAGConfigRequest(BaseModel):
    top_k: int = Field(default=5, ge=1, le=50)
    similarity_threshold: float = Field(default=0.7, ge=0, le=1)
    space_tags: list[str] = Field(default_factory=list)


class PublicKBSearchRequest(BaseModel):
    query_vector: list[float]
    top_k: int = Field(default=5, ge=1, le=50)
    space_tags: list[str] = Field(default_factory=list)


class SyncSummaryRequest(BaseModel):
    summaries: list[dict]
