"""本地项目（方案 A：代码留在用户端，服务器只存结构索引）."""

from typing import Literal

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
    vector_enabled: bool = Field(default=True, description="是否启用代码向量化（涉密项目可关闭）")


class SearchProjectRequest(BaseModel):
    """检索项目索引（定位相关文件）."""

    query: str = Field(..., min_length=1, max_length=200, description="检索关键词")
    limit: int = Field(default=20, ge=1, le=50)


class CodeEmbeddingItem(BaseModel):
    """单条代码向量（本地嵌入后上传，服务器不存代码正文/真实路径）."""

    file_key: str = Field(..., max_length=64, description="相对路径哈希（本地映射回真实路径）")
    function_name: str = Field(default="", max_length=200)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    summary: str = Field(default="", max_length=1000)
    embedding: list[float] = Field(..., description="bge-m3 向量（1024 维）")


class UploadCodeEmbeddingsRequest(BaseModel):
    """批量上传代码向量（全量重建）."""

    items: list[CodeEmbeddingItem] = Field(default_factory=list, max_length=50000)
    mode: Literal["full", "incremental"] = Field(
        default="full",
        description="full=全量重建（先删旧）；incremental=增量追加（本地嵌入分批上传用）",
    )


class CodeChunkItem(BaseModel):
    """客户端分块后的代码块文本（服务端嵌入后即弃，不落库）."""

    file_key: str = Field(..., max_length=64, description="相对路径哈希（本地映射回真实路径）")
    function_name: str = Field(default="", max_length=200)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    summary: str = Field(default="", max_length=1000)
    text: str = Field(..., description="代码块文本（仅用于嵌入，服务端不存储）")


class UploadCodeChunksRequest(BaseModel):
    """批量上传代码块：服务端嵌入 → 存向量 → 文本即用即弃."""

    items: list[CodeChunkItem] = Field(default_factory=list, max_length=50000)
    mode: Literal["full", "incremental"] = Field(
        default="full",
        description="full=全量重建（首次）；incremental=增量追加（只传变更块）",
    )


class UpdateProjectRequest(BaseModel):
    """更新项目（当前仅向量化开关）."""

    vector_enabled: bool = Field(default=True, description="是否启用代码向量化")
