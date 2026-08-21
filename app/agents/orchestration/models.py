"""多智能体协作：任务/状态数据模型."""

import time
from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务节点状态机."""

    PENDING = "pending"          # 已创建，等待依赖完成
    READY = "ready"              # 依赖完成，可执行
    RUNNING = "running"          # 执行中
    PAUSED = "paused"            # 用户暂停（不调度新节点）
    COMPLETED = "completed"      # 成功（通过质检）
    FAILED = "failed"            # 失败（重试耗尽/不可重试）
    RETRYING = "retrying"        # 失败待重试（React 重试）
    INTERRUPTED = "interrupted"  # 被中断（用户终止/断网超时）
    CANCELLED = "cancelled"      # 用户取消
    SKIPPED = "skipped"          # 依赖失败，跳过
    ESCALATED = "escalated"      # 节点已上报，由编排器接管后续裁决


class JobStatus(str, Enum):
    """整个任务的宏观状态."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"      # 全部节点完成（或部分失败但已收敛）
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # 用户终止 / 断网超时
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"  # 等待人工审批
    CONTINUING = "continuing"  # 长清单已完成当前批次，正在物化下一批


class ResourceClaim(BaseModel):
    """节点对外部资源的访问声明；同资源 write 与任何访问互斥。"""

    key: str
    mode: str = "read"  # read / write


class TaskNode(BaseModel):
    """DAG 中的一个任务节点."""

    id: str
    name: str = ""
    agent: str                    # 执行该任务的 WorkerAgent 名
    params: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: dict | None = None
    error: str | None = None
    error_code: str | None = None
    retries: int = 0
    max_retries: int = 2
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    metadata: dict = Field(default_factory=dict)
    resource_claims: list[ResourceClaim] = Field(default_factory=list)
    idempotency_key: str | None = None
    effect_status: str | None = None  # pending / committed / failed / uncertain
    # 审批门控：高风险写操作（发邮件/改系统/付款等）需人工确认后才执行
    approval: bool = False
    approval_note: str = ""


class Job(BaseModel):
    """一次多智能体协作任务（含任务树）."""

    job_id: str
    user_id: str
    user_role: str = "user"
    request: str                  # 用户原始请求
    scene: str = "office"
    conversation_id: str | None = None  # 关联会话（办公短期记忆：跨任务记住上一步做了什么）
    submission_key: str | None = None  # 请求+会话+文档等输入的幂等指纹
    status: JobStatus = JobStatus.PENDING
    nodes: list[TaskNode] = Field(default_factory=list)
    result: dict | None = None    # 汇总结果（如最终回复）
    plan_text: str | None = None  # 规划器产出的执行计划文本（供展示/审计）
    # 办公任务路由与恢复审计信息。使用开放 dict 保持 API 向后兼容，旧任务
    # 快照缺少该字段时会自动使用空对象。
    routing: dict = Field(default_factory=dict)
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    revision: int = 0
    # Execution lineage is immutable after creation. A fork creates a new Job
    # and never overwrites the historical execution the user is comparing.
    execution_id: str = ""
    parent_execution_id: str | None = None
    root_execution_id: str | None = None
    forked_from_node_id: str | None = None


class ReviewVerdict(BaseModel):
    """质检结论."""

    approved: bool = True
    feedback: str = ""
