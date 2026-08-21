"""多智能体协作 API 模型."""

from pydantic import BaseModel, Field


class CreateAgentJobRequest(BaseModel):
    """提交一个多智能体协作任务."""

    request: str = Field(..., min_length=1, max_length=2000, description="用户请求（办公模式）")
    scene: str = Field(default="office", description="场景：office")
    conversation_id: str | None = Field(
        default=None,
        description="关联会话 ID（办公短期记忆：把上一步任务摘要注入后续任务规划）",
    )
    project_id: str | None = Field(
        default=None, description="本地项目 ID（代码任务；缺省时按请求中的项目名匹配）"
    )
    project_ids: list[str] | None = Field(
        default=None,
        description="用户本机已注册的本地项目 ID 列表（代码任务自动定位用；规划器从中自动选择目标项目，支持跨项目顺序修改）",
    )
    clarification_answer: str | None = Field(
        default=None,
        description="指挥层澄清问题的用户回答（重提任务时携带，作为规划上下文）",
    )
    office_docs: list[dict] | None = Field(
        default=None,
        description="当前办公文档会话列表 [{doc_id, filename, kind}]；规划器按文件名匹配，给 office_doc 节点带正确 doc_id",
    )


class CancelAgentJobRequest(BaseModel):
    """终止任务：是否保留已完成节点."""

    keep_completed: bool = Field(default=True, description="保留已完成任务节点")


class ApproveAgentJobRequest(BaseModel):
    """人工审批：批准/拒绝某个高风险节点."""

    node_id: str = Field(..., description="待审批的节点 id")
    approved: bool = Field(default=True, description="true=批准执行，false=拒绝跳过")


class ForkAgentJobRequest(BaseModel):
    """Create a new execution branch from one historical node."""

    node_id: str = Field(..., min_length=1, max_length=200, description="新分支开始执行的节点 id")
    params: dict | None = Field(default=None, description="合并到该节点的受控参数覆盖")
    instruction: str | None = Field(default=None, max_length=4000, description="替换该节点的原子指令")
