"""沙箱抽象 —— 技能代码/命令的隔离执行环境.

职责边界：
  - 沙箱只负责"怎么安全地执行"，不关心业务语义
  - 技能层决定"执行什么"，沙箱层决定"在哪里执行"
  - 默认提供 local 占位实现（未启用时一律拒绝），后期可扩展 docker / wasm / 远程沙箱
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class SandboxResult(BaseModel):
    """沙箱执行结果."""

    status: str = "success"  # success / error / timeout / rejected
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    duration_ms: int = 0
    resource_usage: dict = Field(default_factory=dict)  # exit_code / cpu / memory 等


class Sandbox(ABC):
    """沙箱基类：新增沙箱实现时继承并注册到沙箱注册中心."""

    name: str = "base"

    @abstractmethod
    async def run_script(
        self,
        code: str,
        language: str = "python",
        timeout: int = 30,
    ) -> SandboxResult:
        """在隔离环境执行一段脚本."""
        ...

    @abstractmethod
    async def run_command(
        self,
        cmd: list[str],
        timeout: int = 30,
    ) -> SandboxResult:
        """在隔离环境执行命令."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放沙箱资源（进程/容器等）."""
