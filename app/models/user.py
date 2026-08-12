"""用户模块数据模型."""

from pydantic import BaseModel, Field


class UserProfileUpdateRequest(BaseModel):
    """更新个人资料（部分字段可缺省）."""

    nickname: str | None = Field(default=None, max_length=100, description="昵称")
    avatar_url: str | None = Field(default=None, description="头像（data:image/... base64）")


class ChangePasswordRequest(BaseModel):
    """修改密码."""

    old_password: str = Field(..., description="原密码")
    new_password: str = Field(..., description="新密码")


class SetPromptRequest(BaseModel):
    """设置角色提示词."""

    prompt_id: str = Field(default="", description="角色提示词 id；空串表示恢复场景默认")


class UserLlmConfigRequest(BaseModel):
    """用户模型选择（办公模式）."""

    provider: str = Field(..., description="API 提供商: deepseek / qwen / openai")
    model: str = Field(..., description="模型名称（内置目录 id 或 BYOK 自填模型名）")
    reasoning_effort: str | None = Field(
        default=None,
        description="推理强度: low / medium / high（模型支持时生效）",
    )
    byok: bool = Field(default=False, description="是否自备 API key（key 本地保存，不上传）")
