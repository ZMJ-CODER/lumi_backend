"""知识库模块数据模型."""

from pydantic import BaseModel, Field


class CreateSpaceRequest(BaseModel):
    name: str = Field(..., description="空间名称")
    description: str = ""
    scene_tag: str | None = Field(default=None, description="场景标签: chat / office / game")
    is_public: bool = Field(default=False, description="是否公共空间（仅管理员可设 true）")


class UpdateSpaceRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    scene_tag: str | None = None
    is_public: bool | None = Field(default=None, description="是否公共空间（仅管理员可设 true）")


class AdminPasswordVerifyRequest(BaseModel):
    admin_password: str = Field(..., description="管理员密码")


class RebuildIndexRequest(BaseModel):
    space_id: str | None = None
