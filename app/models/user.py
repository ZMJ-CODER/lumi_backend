"""用户模块数据模型."""

from typing import Literal

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

    provider: str = Field(..., description="OpenAI 兼容 API 提供商；custom 表示自定义地址")
    model: str = Field(..., description="模型名称（内置目录 id 或 BYOK 自填模型名）")
    base_url: str | None = Field(
        default=None,
        max_length=500,
        description="BYOK 的 OpenAI-compatible API 地址（不含 API key）",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description="推理强度: low / medium / high（模型支持时生效）",
    )
    byok: bool = Field(default=False, description="是否自备 API key（key 本地保存，不上传）")


class PreferencesUpdateRequest(BaseModel):
    """用户个性化偏好更新（部分字段可缺省）."""

    avatar: str | None = Field(default=None, description="智能体头像 dataURL")
    background_image: str | None = Field(default=None, description="全局主题背景 dataURL")
    reply_style: Literal["long", "short"] | None = Field(
        default=None, description="回复风格：long=长句 / short=多段短句（仅普通模式）"
    )
    voice: dict | None = Field(
        default=None,
        description="声音设置：{voice, rate, pitch, referenceAudio, referenceName}",
    )
    email_client: str | None = Field(
        default=None,
        max_length=32,
        description="默认邮件客户端：outlook/thunderbird/foxmail/mailmaster 等；空=系统默认",
    )


class PresetCreateRequest(BaseModel):
    """保存个性化方案."""

    kind: Literal["character", "voice"] = Field(..., description="方案类型")
    name: str = Field(..., min_length=1, max_length=100, description="方案名称")
    payload: dict = Field(..., description="方案内容（character: {prompt_id, reply_style?}；voice: 声音设置）")


class DeleteAccountRequest(BaseModel):
    """注销账号：需要输入当前密码确认."""

    password: str = Field(..., description="当前登录密码")
