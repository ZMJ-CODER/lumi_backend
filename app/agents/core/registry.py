"""Agent 集中注册表 —— 所有执行层 agent 统一在这里注册/查询.

集中化管理：编排器（DAG/Temporal）、规划器（可用 agent 列表）、执行路由
都通过本注册表获取 agent，新增 agent 只需在 roles/<领域>/ 下实现并注册。
"""

from __future__ import annotations

from loguru import logger

from app.agents.core.base import WorkerAgent


class AgentRegistry:
    """执行层 agent 注册表（单例，按 name 路由）."""

    _agents: dict[str, WorkerAgent] = {}
    _registered = False

    @classmethod
    def register(cls, agent: WorkerAgent) -> None:
        """注册一个 agent（同名覆盖并告警）."""
        if agent.name in cls._agents:
            logger.warning("Agent '{}' 已存在，将被覆盖", agent.name)
        cls._agents[agent.name] = agent

    @classmethod
    def get(cls, name: str) -> WorkerAgent | None:
        return cls._agents.get(name)

    @classmethod
    def list(cls) -> list[WorkerAgent]:
        return list(cls._agents.values())

    @classmethod
    def names(cls) -> list[str]:
        return list(cls._agents.keys())

    @classmethod
    def all(cls) -> dict[str, WorkerAgent]:
        """返回 name -> instance 的映射（兼容旧 WORKERS dict）."""
        return dict(cls._agents)

    @classmethod
    def clear(cls) -> None:
        cls._agents.clear()
        cls._registered = False


def ensure_registered() -> None:
    """确保 roles 包已导入并注册全部 agent（幂等）."""
    if AgentRegistry._registered:
        return
    from app.agents.roles import register_all_agents  # noqa: PLC0415

    register_all_agents()
    AgentRegistry._registered = True
