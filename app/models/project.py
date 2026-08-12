"""本地项目（方案 A：代码留在用户端，服务器只存结构索引）."""

from pydantic import BaseModel, Field


class ProjectFileIndex(BaseModel):
    """单个文件的结构索引（不含代码正文，机密内容不上传）."""

    path: str = Field(..., max_length=1000, description="项目内相对路径")
    symbols: str = Field(default="", max_length=4000, description="函数/类名等符号（逗号分隔）")
    summary: str = Field(default="", max_length=1000, description="首段注释/文档摘要（截断）")
    size: int = Field(default=0, ge=0, description="文件大小（字节）")


class RegisterProjectRequest(BaseModel):
    """注册本地项目并上传结构索引."""

    name: str = Field(..., min_length=1, max_length=200, description="项目名")
    root_label: str = Field(default="", max_length=500, description="本地根路径摘要（仅展示）")
    files: list[ProjectFileIndex] = Field(default_factory=list, max_length=20000)


class SearchProjectRequest(BaseModel):
    """检索项目索引（定位相关文件）."""

    query: str = Field(..., min_length=1, max_length=200, description="检索关键词")
    limit: int = Field(default=20, ge=1, le=50)
