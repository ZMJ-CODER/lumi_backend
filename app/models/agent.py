"""多智能体协作 API 模型."""

from pydantic import BaseModel, Field


class CreateAgentJobRequest(BaseModel):
    """提交一个多智能体协作任务."""

    request: str = Field(..., min_length=1, max_length=2000, description="用户请求（办公模式）")
    scene: str = Field(default="office", description="场景：office")


class CancelAgentJobRequest(BaseModel):
    """终止任务：是否保留已完成节点."""

    keep_completed: bool = Field(default=True, description="保留已完成任务节点")
