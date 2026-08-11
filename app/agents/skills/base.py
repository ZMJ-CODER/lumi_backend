"""技能抽象 —— 智能体可调用的能力单元.

技能层只定义"智能体能做什么"（名称 / 描述 / 参数 / 执行），
不关心"在哪里执行"——执行环境由沙箱层负责，两者解耦：

  - 智能体（LLM）决定调用哪个技能、传什么参数
  - 技能（Skill）声明能力契约
  - 沙箱（Sandbox）提供隔离的执行环境

技能元数据（供场景过滤 / 权限治理 / 管理分组）:
  - category:      功能域（data_query / web / computation / knowledge / system_op / client_op）
  - environment:   执行环境（server=后端直接执行 / sandbox=本地子进程隔离 / client=推送用户端执行）
  - permission:    权限级别（user / admin）
  - requires_confirmation: 高危操作，执行前需用户确认（client 通道二期实现）
  - scenes:        可用场景白名单（空 = 全场景）
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


class SkillResult(BaseModel):
    """技能执行结果."""

    success: bool
    output: str = ""
    error: str | None = None
    # 标准错误码：SKILL_NOT_FOUND / INVALID_ARGS / NEEDS_CONFIRMATION /
    #              EXEC_ERROR / TIMEOUT / REJECTED
    error_code: str | None = None
    # 网络/瞬时类错误可重试（True 时 LLM 可再调一次），参数类错误不可重试
    retryable: bool = False
    metadata: dict = Field(default_factory=dict)


@dataclass
class SkillContext:
    """技能执行上下文（技能需要的用户/会话信息，由执行器注入，不来自 LLM 参数）."""

    user_id: str = ""
    scene: str = "chat"
    conversation_id: str = ""


class Skill(ABC):
    """技能基类：新增技能时继承并注册到 SkillRegistry."""

    name: str = ""
    description: str = ""
    category: str = "general"           # 功能域，用于分组/过滤
    environment: str = "server"         # server / sandbox / client
    permission: str = "user"            # user / admin
    requires_confirmation: bool = False  # 高危操作需用户确认（client 通道）
    scenes: list[str] = []              # 可用场景白名单，空 = 全场景
    # 参数 JSON Schema（LLM 调用时校验参数用），空 dict 表示无参数
    parameters_schema: dict = Field(default_factory=dict)

    @abstractmethod
    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        """执行技能."""
        ...

    @property
    def requires_sandbox(self) -> bool:
        """是否需要在隔离沙箱中执行（environment == sandbox）."""
        return self.environment == "sandbox"

    def supports_scene(self, scene: str) -> bool:
        """是否支持指定场景."""
        if not self.scenes:
            return True
        return scene in self.scenes

    def to_tool_definition(self) -> dict:
        """转成 OpenAI/Qwen 兼容的 function calling 工具定义."""
        desc = self.description
        if self.requires_confirmation:
            desc += "（高危操作：执行前需要用户确认）"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": self.parameters_schema,
            },
        }

    def __repr__(self) -> str:
        return (
            f"<Skill: {self.name} cat={self.category} env={self.environment} "
            f"permission={self.permission} scenes={self.scenes or 'all'}>"
        )
