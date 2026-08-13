"""执行层 Agent 角色包 —— 按领域拆分，统一注册到 AgentRegistry.

新增 agent 步骤：
  1. 在 roles/<领域>/ 下新建文件，继承 WorkerAgent 并实现 execute()；
  2. 在文件顶部给出 name / description / params_help / skills；
  3. 在本 __init__ 导入并加入 register_all_agents() 的实例列表。

注册后：编排器（DAG/Temporal）自动路由、规划器自动列出、前端进度自动显示。
"""

from app.agents.core.base import WorkerAgent
from app.agents.core.registry import AgentRegistry

from app.agents.roles.knowledge.retrieval import RetrievalAgent
from app.agents.roles.code.agent import CodeAgent
from app.agents.roles.code.reader import CodeReaderAgent
from app.agents.roles.code.writer import CodeWriterAgent
from app.agents.roles.code.tester import CodeTesterAgent
from app.agents.roles.code.reviewer import CodeReviewerAgent


def register_all_agents() -> list[WorkerAgent]:
    """注册全部内置执行 agent（幂等：同名覆盖）."""
    instances = [
        RetrievalAgent(),
        CodeAgent(),
        CodeReaderAgent(),
        CodeWriterAgent(),
        CodeTesterAgent(),
        CodeReviewerAgent(),
    ]
    for agent in instances:
        AgentRegistry.register(agent)
    return instances


__all__ = [
    "register_all_agents",
    "RetrievalAgent",
    "CodeAgent",
    "CodeReaderAgent",
    "CodeWriterAgent",
    "CodeTesterAgent",
    "CodeReviewerAgent",
]
