"""智能体注册中心 —— 管理所有智能体的注册与场景路由."""

from loguru import logger

from app.agents.base import AgentBase


class AgentRegistry:
    """智能体注册表（单例）.

    使用方式:
      @AgentRegistry.register
      class MyAgent(AgentBase):
          name = "my_agent"
          ...
    """

    _agents: dict[str, AgentBase] = {}

    @classmethod
    def register(cls, agent: AgentBase) -> None:
        """注册一个智能体."""
        if agent.name in cls._agents:
            logger.warning(f"智能体 '{agent.name}' 已存在，将被覆盖")
        cls._agents[agent.name] = agent
        logger.info(f"智能体已注册: {agent.name} (场景: {agent.supported_scenes or '全部'})")

    @classmethod
    def get(cls, name: str) -> AgentBase | None:
        """按名称获取智能体."""
        return cls._agents.get(name)

    @classmethod
    def get_for_scene(cls, scene: str) -> list[AgentBase]:
        """获取指定场景下可用的所有智能体."""
        return [a for a in cls._agents.values() if a.supports_scene(scene)]

    @classmethod
    def list_all(cls) -> list[AgentBase]:
        """列出所有已注册的智能体."""
        return list(cls._agents.values())

    @classmethod
    def clear(cls) -> None:
        """清空注册表（主要用于测试）."""
        cls._agents.clear()


def init_agents() -> None:
    """初始化：导入所有智能体模块以触发注册.

    新增智能体时在此添加 import:
      from app.agents.chat_agent import ChatAgent
      from app.agents.office_agent import OfficeAgent
      from app.agents.game_agent import GameAgent
    """
    from app.agents.chat_agent import ChatAgent  # noqa: F401
