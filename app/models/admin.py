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
    query: str = Field(default="", description="查询文本（可选）；提供时启用混合检索（向量 + 关键词）")


class SyncSummaryRequest(BaseModel):
    summaries: list[dict]


class LLMConfigRequest(BaseModel):
    """更新 LLM 动态配置（部分字段可缺省，缺省则沿用当前生效值）."""

    scene: str | None = Field(default=None, description="场景标识；缺省表示全局默认")
    base_url: str | None = Field(default=None, description="OpenAI 兼容接口地址")
    api_key: str | None = Field(default=None, description="API 密钥")
    model: str | None = Field(default=None, description="模型名")
    timeout: int | None = Field(default=None, ge=5, le=600, description="请求超时秒数")


class LLMResetRequest(BaseModel):
    """重置 LLM 动态配置，回落 .env 默认值."""

    scene: str | None = Field(default=None, description="场景标识；缺省表示全局默认")
