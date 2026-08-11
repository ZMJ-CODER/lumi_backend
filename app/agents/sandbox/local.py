"""本地沙箱 —— 子进程隔离执行（开发期实现）.

限制（开发期）：
  - 仅允许运行 Python（python -I 隔离模式，不加载用户 site-packages）
  - 临时工作目录 + 环境变量最小化
  - 超时强杀 + 输出截断
  - POSIX 下用 resource 限制内存/CPU；Windows 跳过（生产部署在 Linux 容器）
  - run_command 仅允许白名单命令（当前为空 → 一律拒绝）

注意：本地子进程不是完全隔离（可读文件系统），生产环境如需严格隔离，
在沙箱注册中心注册 docker 实现即可，接口不变。
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time

from app.agents.sandbox.base import Sandbox, SandboxResult
from app.core.config import settings


def _limit_resources() -> None:
    """POSIX 下限制子进程资源（内存/CPU）；Windows 无 resource 模块则跳过."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))  # 512MB
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))  # 60s CPU
    except (ImportError, ValueError, OSError):
        pass


class LocalSandbox(Sandbox):
    """本地子进程沙箱."""

    name = "local"

    async def run_script(self, code: str, language: str = "python", timeout: int = 30) -> SandboxResult:
        if language != "python":
            return SandboxResult(
                status="rejected",
                error=f"本地沙箱暂不支持语言: {language}（仅支持 python）",
                resource_usage={"op": "run_script", "sandbox": self.name},
            )
        t0 = time.monotonic()
        workdir = tempfile.mkdtemp(prefix="lumi_sandbox_")
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": workdir,
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        cmd = [sys.executable, "-I", "-c", code]
        try:
            # preexec_fn 仅 POSIX 支持（生产 Linux 容器启用资源限制；Windows 开发机跳过）
            proc_kwargs = {}
            if os.name == "posix":
                proc_kwargs["preexec_fn"] = _limit_resources
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **proc_kwargs,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    status="timeout",
                    error=f"代码执行超时（>{timeout}s）",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    resource_usage={"exit_code": proc.returncode, "sandbox": self.name},
                )
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(
                status="error",
                error=f"启动沙箱进程失败: {exc}",
                duration_ms=int((time.monotonic() - t0) * 1000),
                resource_usage={"sandbox": self.name},
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        max_chars = settings.AGENT_SANDBOX_MAX_OUTPUT_CHARS
        truncated = len(out) > max_chars or len(err) > max_chars
        return SandboxResult(
            status="success" if proc.returncode == 0 else "error",
            stdout=out[:max_chars],
            stderr=err[:max_chars],
            error=None if proc.returncode == 0 else (err[:500] or f"退出码 {proc.returncode}"),
            duration_ms=int((time.monotonic() - t0) * 1000),
            resource_usage={
                "exit_code": proc.returncode,
                "truncated": truncated,
                "sandbox": self.name,
            },
        )

    async def run_command(self, cmd: list[str], timeout: int = 30) -> SandboxResult:
        # 命令白名单：当前为空 → 一律拒绝。
        # 需要命令执行时，在这里显式列出允许的可执行文件（如 /usr/bin/ffmpeg）。
        whitelist: list[str] = []
        executable = (cmd or [""])[0]
        if executable not in whitelist:
            reason = (
                f"命令 '{executable}' 不在沙箱白名单内，执行被拒绝。"
                f"允许列表: {whitelist or '（空，暂不支持命令执行）'}"
            )
            return SandboxResult(
                status="rejected",
                stderr=reason,
                error=reason,
                resource_usage={"op": "run_command", "sandbox": self.name},
            )
        # 白名单命中后走受限子进程执行（MVP 暂不开放任何命令，预留实现）
        return SandboxResult(
            status="rejected",
            error="命令执行通道暂未开放",
            resource_usage={"op": "run_command", "sandbox": self.name},
        )
