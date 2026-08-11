"""角色提示词数据模型."""

from pydantic import BaseModel, Field


class CreatePromptRequest(BaseModel):
    """创建自定义角色."""

    name: str = Field(..., min_length=1, max_length=100, description="角色名称/标题")
    description: str = Field(default="", max_length=500, description="一句话简介")
    content: str = Field(..., min_length=1, description="角色提示词正文（背景/性格/说话方式等）")
