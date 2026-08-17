"""执行层 Agent 门面 —— 兼容旧导入路径（集中化管理后仅做汇总导出）.

实际实现已拆分到：
  - 抽象基类 / WorkerContext：app.agents.core.base
  - 集中注册表：app.agents.core.registry（AgentRegistry）
  - 公共工具：app.agents.core.tools
  - 各领域 agent：app.agents.roles（code / knowledge / 未来其他领域）

本模块保证 orchestrator / Temporal activities / 测试的旧导入不变。
"""

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.registry import AgentRegistry, ensure_registered
from app.agents.core.tools import (
    CODE_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    TEST_SYSTEM_PROMPT,
    generate_code_content,
    list_project_files,
    locate_project_file,
    review_code_content,
)

# 确保 roles 包已导入并注册全部 agent（幂等）
ensure_registered()

# 兼容旧代码：name -> instance 映射（同 AgentRegistry.all()）
WORKERS: dict[str, WorkerAgent] = AgentRegistry.all()


def get_worker(name: str) -> WorkerAgent | None:
    """按名称获取执行 agent（集中注册表）."""
    return AgentRegistry.get(name)


def list_workers() -> list[WorkerAgent]:
    """列出全部已注册执行 agent."""
    return AgentRegistry.list()


__all__ = [
    "WorkerAgent",
    "WorkerContext",
    "WORKERS",
    "get_worker",
    "list_workers",
    "locate_project_file",
    "list_project_files",
    "generate_code_content",
    "review_code_content",
    "CODE_SYSTEM_PROMPT",
    "TEST_SYSTEM_PROMPT",
    "REVIEW_SYSTEM_PROMPT",
]
