"""多智能体协作编排（办公模式）.

三层架构：
  指挥层  Planner        → 意图拆解 → 任务树（DAG）
  执行层  WorkerAgent    → 领取任务，调用技能执行（React 多轮）
  工具层  技能插件        → 原子能力（web_search / query_knowledge / 本地文件等）

另含：质检钩子（Review）、任务状态机（TaskStatus）、DAG 编排器（execute_dag）。
"""

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus

# 明确导出类和单例。仅依赖 ``__getattr__`` 在存在同名子模块
# ``orchestrator.py`` 时并不可靠：Python 的 from-package 导入可能返回模块
# 对象，导致调用方看到 ``orchestrator.submit_job``/``list_jobs`` 不存在。
# 编排包本身就是应用边界，启动时加载这一单例比运行中静默拿错对象更安全。
from app.agents.orchestration.orchestrator import AgentOrchestrator, orchestrator

__all__ = [
    "AgentOrchestrator",
    "orchestrator",
    "Job",
    "JobStatus",
    "TaskNode",
    "TaskStatus",
]
