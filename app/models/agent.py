"""多智能体协作 API 模型."""

from pydantic import BaseModel, Field


class CreateAgentJobRequest(BaseModel):
    """提交一个多智能体协作任务."""

    request: str = Field(..., min_length=1, max_length=2000, description="用户请求（办公模式）")
    scene: str = Field(default="office", description="场景：office")
    project_id: str | None = Field(
        default=None, description="本地项目 ID（代码任务；缺省时按请求中的项目名匹配）"
    )
    clarification_answer: str | None = Field(
        default=None,
        description="指挥层澄清问题的用户回答（重提任务时携带，作为规划上下文）",
    )


class CancelAgentJobRequest(BaseModel):
    """终止任务：是否保留已完成节点."""

    keep_completed: bool = Field(default=True, description="保留已完成任务节点")
