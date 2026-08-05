"""通用响应模型."""

from pydantic import BaseModel


class BaseResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: object | None = None
