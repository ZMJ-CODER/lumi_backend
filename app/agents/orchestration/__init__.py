"""多智能体协作编排（办公模式）.

三层架构：
  指挥层  Planner        → 意图拆解 → 任务树（DAG）
  执行层  WorkerAgent    → 领取任务，调用技能执行（React 多轮）
  工具层  技能插件        → 原子能力（web_search / query_knowledge / 本地文件等）

另含：质检钩子（Review）、任务状态机（TaskStatus）、DAG 编排器（execute_dag）。
"""

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator, orchestrator

__all__ = [
    "AgentOrchestrator",
    "orchestrator",
    "Job",
    "JobStatus",
    "TaskNode",
    "TaskStatus",
]
