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


class ModelProfileRequest(BaseModel):
    """更新一个模型档位（main/cheap/reasoning/vision）—— 兼容别名接口用。

    只填部分字段时沿用该档位当前生效值；``test_only`` 仅测连接不写入，
    ``reset`` 清除动态覆盖回落 .env。
    """

    provider: str | None = Field(default=None, description="qwen / deepseek / 自定义")
    base_url: str | None = Field(default=None, description="OpenAI 兼容接口地址")
    api_key: str | None = Field(default=None, description="API 密钥（只写不回显）")
    model: str | None = Field(default=None, description="模型名")
    timeout: float | None = Field(default=None, ge=5, le=900, description="请求超时秒数")
    check_connection: bool = Field(default=True, description="写入前验证连通性")
    test_only: bool = Field(default=False, description="只测连接，不写入")
    reset: bool = Field(default=False, description="清除动态覆盖，回落 .env")
    max_output_tokens: int | None = Field(default=None, description="最大输出 token")
    max_context_tokens: int | None = Field(default=None, description="最大上下文 token")
    supports_tools: bool | None = Field(default=None, description="是否支持工具调用")
    supports_json: bool | None = Field(default=None, description="是否支持 JSON 输出")
    supports_vision: bool | None = Field(default=None, description="是否支持视觉")
    supports_reasoning: bool | None = Field(default=None, description="是否允许 reasoning 参数")


class ModelRoleRequest(BaseModel):
    """把一个逻辑角色固定到某个档位（空值 = 清除，回落 LLM_ROLE_*）."""

    profile: str | None = Field(default=None, description="main / cheap / reasoning / vision；空=默认")


class ModelRolesUpdateRequest(BaseModel):
    """前端管理页保存（``PUT /admin/llm-config/models``）。

    只提交被改动的部分：``profiles`` 是「档位 → 字段子集」，``roles`` 是「角色 → 档位名」。
    """

    profiles: dict[str, dict] | None = Field(
        default=None,
        description='档位覆盖，如 {"cheap": {"model": "qwen-turbo", "api_key": "sk-..."}}',
    )
    roles: dict[str, str] | None = Field(
        default=None,
        description='角色映射，如 {"title": "cheap", "code_writer": "main"}',
    )


class ModelRolesResetRequest(BaseModel):
    """重置为 .env 默认：给了 ``profile`` 只重置该档位，否则清空全部动态覆盖."""

    profile: str | None = Field(default=None, description="main / cheap / reasoning / vision")


class ModelRolesTestRequest(BaseModel):
    """连通性测试（指定档位；不返回明文密钥）."""

    profile: str = Field(description="main / cheap / reasoning / vision")


class StrategyPolicyToggleRequest(BaseModel):
    """动态启用或卸载一条已校验的策略文件。"""

    policy_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_-]{0,79}$")
