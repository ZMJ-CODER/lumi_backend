"""技能抽象 —— 智能体可调用的能力单元.

技能层只定义"智能体能做什么"（名称 / 描述 / 参数 / 执行），
不关心"在哪里执行"——执行环境由沙箱层负责，两者解耦：

  - 智能体（LLM）决定调用哪个技能、传什么参数
  - 技能（Skill）声明能力契约
  - 沙箱（Sandbox）提供隔离的执行环境
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class SkillResult(BaseModel):
    """技能执行结果."""

    success: bool
    output: str = ""
    error: str | None = None
    metadata: dict = Field(default_factory=dict)


class Skill(ABC):
    """技能基类：新增技能时继承并注册到 SkillRegistry."""

    name: str = ""
    description: str = ""
    # 参数 JSON Schema（LLM 调用时校验参数用），空 dict 表示无参数
    parameters_schema: dict = Field(default_factory=dict)
    # 是否需要沙箱执行（如执行代码/命令），纯查询类技能可设 False
    requires_sandbox: bool = True

    @abstractmethod
    async def execute(self, params: dict) -> SkillResult:
        """执行技能."""
        ...
