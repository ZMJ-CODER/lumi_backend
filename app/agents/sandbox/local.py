"""本地沙箱 —— 占位实现（预留）.

当前状态：
  - AGENT_SKILLS_ENABLED=false 时，所有执行请求一律拒绝（返回 rejected）
  - 后期启用后，在这里实现"子进程 + 资源限制（内存/CPU/超时/输出截断）"，
    或切换到 docker / wasm 沙箱（在 registry 注册对应实现即可）。
"""

import time

from app.agents.sandbox.base import Sandbox, SandboxResult
from app.core.config import settings


class LocalSandbox(Sandbox):
    """本地子进程沙箱（占位，未启用时拒绝执行）."""

    name = "local"

    async def run_script(self, code: str, language: str = "python", timeout: int = 30) -> SandboxResult:
        return self._reject("run_script", language)

    async def run_command(self, cmd: list[str], timeout: int = 30) -> SandboxResult:
        return self._reject("run_command", cmd)

    def _reject(self, op: str, target) -> SandboxResult:
        t0 = time.monotonic()
        reason = "技能沙箱未启用（AGENT_SKILLS_ENABLED=false），执行被拒绝"
        return SandboxResult(
            status="rejected",
            stderr=reason,
            error=reason,
            duration_ms=int((time.monotonic() - t0) * 1000),
            resource_usage={"op": op, "sandbox": self.name},
        )
