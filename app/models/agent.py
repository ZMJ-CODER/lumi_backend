"""多智能体协作 API 模型."""

from pydantic import BaseModel, Field
from lumi_orch import ExpansionSlot, NodeSpec, PlanPatch


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
        description="当前办公文档会话列表 [{doc_id, filename, kind}]；规划器按文件名匹配，为真实文档工具带上正确 doc_id",
    )
    workspace_id: str | None = Field(
        default=None,
        description="兼容字段；正常情况下由 conversation_id 自动解析唯一工作区",
    )
    execution_preference: str = Field(
        default="use_workspace_policy",
        description="办公任务执行方式：use_workspace_policy / step_confirm / auto_routine",
    )
    timeout_seconds: float | None = Field(
        default=None,
        ge=1,
        le=600,
        description=(
            "可选：本次任务的内部执行超时（秒）。缺省时按复杂度档位取超时阶梯"
            "（M0=5/M1=10/M2=30/M3=60，随模型计划升级，可在配置里调整）。"
            "只影响内部等待的有界化，不改变任务语义。"
        ),
    )


RESUME_ACTION_RESUME = "resume"
RESUME_ACTION_RUN_NEXT = "run_next"
RESUME_ACTIONS = frozenset({RESUME_ACTION_RESUME, RESUME_ACTION_RUN_NEXT})


class ResumeAgentJobRequest(BaseModel):
    """恢复任务 / 单步执行（run_next）.

    - action=resume：恢复被暂停的任务（默认，兼容旧调用方，无需请求体）。
    - action=run_next：step_confirm 计划优先任务的“运行下一步”：
      后端校验通过后从 routing.steps/current_step_index 定位步骤，执行
      Job.nodes 中同 id 的 TaskNode，并以 SSE 事件流返回本步执行过程与
      落点（waiting_next / waiting_approval / task_completed / task_failed）。
    """

    action: str = Field(default=RESUME_ACTION_RESUME, description="resume | run_next")
    expected_step_id: str = Field(
        default="",
        max_length=200,
        description="run_next：客户端持有的当前步骤 id（用于并发/陈旧视图检测；空则取服务端 current_step_index）",
    )
    plan_revision: int | None = Field(
        default=None,
        ge=1,
        description="run_next：客户端持有的计划版本；缺省时兼容旧客户端并由服务端取当前版本",
    )
    idempotency_key: str = Field(
        default="",
        max_length=200,
        description="run_next：本轮步骤执行的幂等键（同一键不重复执行）",
    )


class CancelAgentJobRequest(BaseModel):
    """终止任务：是否保留已完成节点/步骤与暂存成果."""

    reason: str = Field(default="user_cancelled", max_length=200, description="取消原因（user_cancelled 等）")
    keep_completed: bool = Field(default=True, description="保留已完成任务节点")
    keep_completed_steps: bool | None = Field(
        default=None,
        description="前端契约名 keepCompletedSteps：优先于 keep_completed（默认保留已完成步骤与暂存成果）",
    )


class ApproveAgentJobRequest(BaseModel):
    """人工审批：批准/拒绝某个高风险节点."""

    node_id: str = Field(..., description="待审批的节点 id")
    approved: bool = Field(default=True, description="true=批准执行，false=拒绝跳过")


class ForkAgentJobRequest(BaseModel):
    """Create a new execution branch from one historical node."""

    node_id: str = Field(..., min_length=1, max_length=200, description="新分支开始执行的节点 id")
    params: dict | None = Field(default=None, description="合并到该节点的受控参数覆盖")
    instruction: str | None = Field(default=None, max_length=4000, description="替换该节点的原子指令")


class AppendPlanPatchRequest(BaseModel):
    """外部系统向已就绪骨架插槽追加受限节点。"""

    patch_id: str = Field(..., min_length=1, max_length=160)
    slot_id: str = Field(..., min_length=1, max_length=160)
    base_revision: int = Field(..., ge=1)
    nodes: list[NodeSpec] = Field(default_factory=list, max_length=64)
    slots: list[ExpansionSlot] = Field(default_factory=list, max_length=16)

    def as_external_patch(self) -> PlanPatch:
        return PlanPatch(
            patch_id=self.patch_id,
            slot_id=self.slot_id,
            base_revision=self.base_revision,
            source="external",
            nodes=tuple(self.nodes),
            slots=tuple(self.slots),
        )
