"""沙箱层（预留）—— 技能代码/命令的隔离执行环境.

架构约定：
  - 智能体/技能层只描述"要做什么"
  - 沙箱层负责"在哪里执行、如何隔离"
  - 通过沙箱注册中心按类型获取实例，类型由配置 AGENT_SANDBOX_TYPE 决定
"""

from app.agents.sandbox.base import Sandbox, SandboxResult
from app.agents.sandbox.registry import available_sandboxes, get_sandbox, register_sandbox

__all__ = ["Sandbox", "SandboxResult", "get_sandbox", "register_sandbox", "available_sandboxes"]
