"""执行层 Agent 角色包 —— 按领域拆分，统一注册到 AgentRegistry.

新增 agent 步骤：
  1. 在 roles/<领域>/ 下新建文件，继承 WorkerAgent 并实现 execute()；
  2. 在文件顶部给出 name / description / params_help / skills；
  3. 在本 __init__ 导入并加入 register_all_agents() 的实例列表。

注册后：编排器（DAG/Temporal）自动路由、规划器自动列出、前端进度自动显示。
"""

from app.agents.core.base import WorkerAgent
from app.agents.core.registry import AgentRegistry
from app.core.config import settings
from loguru import logger

from app.agents.roles.knowledge.retrieval import RetrievalAgent
from app.agents.roles.knowledge.web_research import WebResearchAgent
from app.agents.roles.atomic import AtomicStepAgent
from app.agents.roles.react import ReactStepAgent
from app.agents.roles.office.agents import (
    OfficeCalendarAgent,
    OfficeDocAgent,
    OfficeResearchAgent,
    OfficeScriptAgent,
    OfficeSystemAgent,
    OfficeTextAgent,
    OfficeTodoAgent,
)
from app.agents.roles.code.agent import CodeAgent
from app.agents.roles.code.reader import CodeReaderAgent
from app.agents.roles.code.writer import CodeWriterAgent
from app.agents.roles.code.tester import CodeTesterAgent
from app.agents.roles.code.reviewer import CodeReviewerAgent


def register_all_agents() -> list[WorkerAgent]:
    """注册内置执行 agent（按 AGENT_DISABLED 过滤；幂等：同名覆盖）."""
    instances = [
        AtomicStepAgent(),
        ReactStepAgent(),
        RetrievalAgent(),
        WebResearchAgent(),
        OfficeTextAgent(),
        OfficeResearchAgent(),
        OfficeTodoAgent(),
        OfficeCalendarAgent(),
        OfficeDocAgent(),
        OfficeScriptAgent(),
        OfficeSystemAgent(),
        CodeAgent(),
        CodeReaderAgent(),
        CodeWriterAgent(),
        CodeTesterAgent(),
        CodeReviewerAgent(),
    ]
    disabled = set(settings.AGENT_DISABLED or [])
    registered: list[WorkerAgent] = []
    for agent in instances:
        if agent.name in disabled:
            logger.info("Agent '{}' 已在 AGENT_DISABLED 中，跳过注册", agent.name)
            continue
        AgentRegistry.register(agent)
        registered.append(agent)
    return registered


__all__ = [
    "register_all_agents",
    "AtomicStepAgent",
    "RetrievalAgent",
    "WebResearchAgent",
    "OfficeTextAgent",
    "OfficeResearchAgent",
    "OfficeTodoAgent",
    "OfficeDocAgent",
    "OfficeScriptAgent",
    "CodeAgent",
    "CodeReaderAgent",
    "CodeWriterAgent",
    "CodeTesterAgent",
    "CodeReviewerAgent",
]
