"""智能体基类 —— 支持场景化人格."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentContext:
    """智能体执行上下文."""

    user_id: str = ""
    session_id: str | None = None
    scene: str = "chat"  # chat / office / game
    memory_facts: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class AgentBase(ABC):
    """所有智能体必须继承此基类.

    子类需定义:
      - name:        智能体唯一标识
      - description: 智能体描述
      - supported_scenes: 支持的场景列表（空 = 全场景通用）

    子类需实现:
      - execute(message, context) → str
    """

    name: str = ""
    description: str = ""
    supported_scenes: list[str] = []  # 空列表表示支持所有场景

    @abstractmethod
    async def execute(self, message: str, context: AgentContext) -> str:
        """执行智能体逻辑，返回响应文本."""
        ...

    def supports_scene(self, scene: str) -> bool:
        """判断是否支持指定场景."""
        if not self.supported_scenes:
            return True
        return scene in self.supported_scenes

    def __repr__(self) -> str:
        return f"<Agent: {self.name} scenes={self.supported_scenes or 'all'}>"
