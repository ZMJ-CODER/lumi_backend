"""知识库模块数据模型."""

from pydantic import BaseModel, Field


class CreateSpaceRequest(BaseModel):
    name: str = Field(..., description="空间名称")
    description: str = ""


class UpdateSpaceRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class AdminPasswordVerifyRequest(BaseModel):
    admin_password: str = Field(..., description="管理员密码")


class RebuildIndexRequest(BaseModel):
    space_id: str | None = None
