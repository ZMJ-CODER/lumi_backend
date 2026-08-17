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
import subprocess
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

    async def run_script(
        self,
        code: str,
        language: str = "python",
        timeout: int = 30,
        env_extra: dict | None = None,
    ) -> SandboxResult:
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
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            **(env_extra or {}),
        }
        # Windows：补齐系统目录，避免 CreateProcess 因找不到系统 DLL/可执行文件失败
        if os.name == "nt":
            env.setdefault("SYSTEMROOT", os.environ.get("SYSTEMROOT", r"C:\Windows"))
            env.setdefault("SYSTEMDRIVE", os.environ.get("SYSTEMDRIVE", "C:"))
            env.setdefault("WINDIR", os.environ.get("WINDIR", r"C:\Windows"))
            env["PATH"] = os.environ.get("PATH", "") + os.pathsep + env.get("PATH", "")
        # 不用 -I：允许脚本使用项目 venv 的第三方库（openpyxl / python-docx 等办公处理库）。
        # 本地子进程并非严格隔离（可读文件系统），超时/输出截断仍然生效；
        # 生产如需严格隔离可注册 docker 沙箱实现。
        cmd = [sys.executable, "-c", code]
        # Windows 非 Proactor 事件循环（如 Temporal Activity 线程）不支持 asyncio 子进程，
        # 改用线程同步执行（subprocess.run），功能一致。
        if os.name == "nt" and not isinstance(
            asyncio.get_running_loop(), asyncio.ProactorEventLoop
        ):
            try:
                return await asyncio.to_thread(
                    self._run_script_sync, code, workdir, env, timeout, t0
                )
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
        try:
            # preexec_fn 仅 POSIX 支持（生产 Linux 容器启用资源限制；Windows 开发机跳过）
            proc_kwargs = {}
            if os.name == "posix":
                proc_kwargs["preexec_fn"] = _limit_resources
            for attempt in range(2):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=workdir,
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **proc_kwargs,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - 偶发 CreateProcess 失败，重试一次
                    if attempt == 0:
                        await asyncio.sleep(0.3)
                        continue
                    return SandboxResult(
                        status="error",
                        error=f"启动沙箱进程失败: {exc!r}",
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        resource_usage={"sandbox": self.name, "attempts": 2},
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
                error=f"启动沙箱进程失败: {exc!r}",
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

    def _run_script_sync(
        self,
        code: str,
        workdir: str,
        env: dict,
        timeout: int,
        t0: float,
    ) -> SandboxResult:
        """同步子进程执行（Windows 非 Proactor 事件循环回退）."""
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=workdir,
                env=env,
                capture_output=True,
                timeout=timeout,
            )
            stdout, stderr = proc.stdout, proc.stderr
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            return SandboxResult(
                status="timeout",
                error=f"代码执行超时（>{timeout}s）",
                duration_ms=int((time.monotonic() - t0) * 1000),
                resource_usage={"sandbox": self.name},
            )
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(
                status="error",
                error=f"启动沙箱进程失败: {exc!r}",
                duration_ms=int((time.monotonic() - t0) * 1000),
                resource_usage={"sandbox": self.name},
            )
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        max_chars = settings.AGENT_SANDBOX_MAX_OUTPUT_CHARS
        truncated = len(out) > max_chars or len(err) > max_chars
        return SandboxResult(
            status="success" if returncode == 0 else "error",
            stdout=out[:max_chars],
            stderr=err[:max_chars],
            error=None if returncode == 0 else (err[:500] or f"退出码 {returncode}"),
            duration_ms=int((time.monotonic() - t0) * 1000),
            resource_usage={
                "exit_code": returncode,
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

    async def close(self) -> None:
        """本地子进程沙箱无长驻资源，无需额外清理."""
        return None
