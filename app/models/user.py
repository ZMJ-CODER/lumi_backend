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
