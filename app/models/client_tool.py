"""客户端工具请求模型."""

from pydantic import BaseModel, Field


class ClientToolResultRequest(BaseModel):
    """用户端执行结果回传."""

    success: bool = Field(..., description="是否执行成功")
    output: str = Field(default="", description="执行输出（文件内容/目录列表等）")
    error: str | None = Field(default=None, description="失败原因")
    metadata: dict = Field(default_factory=dict, description="附加信息（如错误码）")
