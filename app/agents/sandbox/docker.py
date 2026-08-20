"""Docker 隔离脚本沙箱。

每次调用创建一次性容器。输入在容器启动后经受控 ``docker exec`` 标准输入写入
唯一可写的 tmpfs 工作区；输出再复制回调用方明确授权的目录。因此脚本容器不持有任何宿主 bind mount，也看不到后端源码、
配置、数据库连接或其他用户文件。容器禁网、只读根文件系统、无 capabilities，
并受 CPU / 内存 / PID / 文件描述符 / 运行时间限制。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path, PurePosixPath

from app.agents.sandbox.base import Sandbox, SandboxResult
from app.core.config import settings


class DockerSandbox(Sandbox):
    name = "docker"
    _availability_cache: tuple[float, bool, str] | None = None

    @staticmethod
    def _docker_cmd() -> str:
        return settings.AGENT_SANDBOX_DOCKER_BINARY or "docker"

    def is_available(self) -> tuple[bool, str]:
        """检查 Docker daemon 与固定镜像；短缓存避免每次规划都 fork 命令。"""
        cached = self._availability_cache
        now = time.monotonic()
        if cached and now - cached[0] < 5:
            return cached[1], cached[2]
        try:
            proc = subprocess.run(
                [self._docker_cmd(), "image", "inspect", settings.AGENT_SANDBOX_DOCKER_IMAGE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            if proc.returncode == 0:
                value = (True, "")
            else:
                detail = proc.stderr.decode("utf-8", errors="replace").strip()
                value = (False, f"Docker 沙箱镜像不可用：{detail[:180] or settings.AGENT_SANDBOX_DOCKER_IMAGE}")
        except FileNotFoundError:
            value = (False, "服务器未安装 Docker CLI")
        except subprocess.TimeoutExpired:
            value = (False, "检查 Docker 沙箱超时")
        except Exception as exc:  # noqa: BLE001
            value = (False, f"Docker 沙箱不可用：{type(exc).__name__}")
        self._availability_cache = (now, *value)
        return value

    @staticmethod
    def _validated_transfer(item: dict[str, str]) -> tuple[Path, str, bool]:
        source = Path(str(item.get("source") or "")).resolve()
        target = PurePosixPath(str(item.get("target") or ""))
        writable = str(item.get("mode") or "ro").lower() == "rw"
        if not source.exists():
            raise ValueError("授权的沙箱文件不存在")
        if not target.is_absolute() or ".." in target.parts or not str(target).startswith("/workspace/"):
            raise ValueError("沙箱文件目标不安全")
        if writable and not source.is_dir():
            raise ValueError("沙箱可写目标必须是目录")
        return source, str(target), writable

    @staticmethod
    def _clean_detail(stderr: bytes) -> str:
        return stderr.decode("utf-8", errors="replace").strip()[:500]

    async def _run_cli(self, *args: str, stdin: bytes | None = None, timeout: int = 10) -> tuple[int, bytes, bytes]:
        """调用 Docker CLI，继承最小环境，不能执行 shell 拼接。"""
        env = {"PATH": os.environ.get("PATH", "")}
        if os.environ.get("DOCKER_HOST"):
            env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
        proc = await asyncio.create_subprocess_exec(
            self._docker_cmd(), *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode, stdout, stderr

    async def _copy_input(self, container_id: str, source: Path, target: str) -> tuple[int, bytes, bytes]:
        """把已授权输入写入运行中容器的 /workspace tmpfs。

        ``docker cp`` 在 ``--read-only`` 容器中仍会按 rootfs 写入处理，Docker
        daemon 因而拒绝它，即使目标是 tmpfs。这里使用固定的 ``cat > $target``
        命令，并且 target 已由 ``_validated_transfer`` 限定在 /workspace 下。
        """
        if source.is_dir():
            status, out, err = await self._run_cli(
                "exec", "--workdir", "/workspace",
                "--user", f"{settings.AGENT_SANDBOX_DOCKER_UID}:{settings.AGENT_SANDBOX_DOCKER_GID}",
                container_id, "/bin/sh", "-c", 'mkdir -p "$1"', "--", target,
                timeout=10,
            )
            if status != 0:
                return status, out, err
            root = source.resolve()
            for item in source.rglob("*"):
                # 不跟随目录内链接，防止授权目录中的链接越界读取宿主文件。
                if not item.is_file() or item.is_symlink():
                    continue
                resolved = item.resolve()
                if not resolved.is_relative_to(root):
                    return 1, b"", b"sandbox input symlink escapes source directory"
                relative = item.relative_to(source).as_posix()
                status, out, err = await self._copy_input(container_id, item, str(PurePosixPath(target) / relative))
                if status != 0:
                    return status, out, err
            return 0, b"", b""

        payload = await asyncio.to_thread(source.read_bytes)
        return await self._run_cli(
            "exec", "-i", "--workdir", "/workspace",
            "--user", f"{settings.AGENT_SANDBOX_DOCKER_UID}:{settings.AGENT_SANDBOX_DOCKER_GID}",
            container_id, "/bin/sh", "-c", 'mkdir -p "$(dirname "$1")"; cat > "$1"', "--", target,
            stdin=payload,
            timeout=10,
        )

    async def _prepare_output_dirs(self, container_id: str, targets: list[str]) -> tuple[int, bytes, bytes]:
        if not targets:
            return 0, b"", b""
        return await self._run_cli(
            "exec", "--workdir", "/workspace",
            "--user", f"{settings.AGENT_SANDBOX_DOCKER_UID}:{settings.AGENT_SANDBOX_DOCKER_GID}",
            container_id, "/bin/sh", "-c", 'mkdir -p "$@"', "--", *targets,
            timeout=10,
        )

    async def _copy_output_tree(self, container_id: str, source: Path, target: str) -> None:
        """受控地把 tmpfs 产物读回调用方授权的目录。

        不能使用 ``docker cp``：Docker 对只读 rootfs + tmpfs 的 copy 语义在不同
        daemon 版本中并不一致，且历史上会静默丢失产物。这里逐个枚举普通文件并
        以 ``docker exec cat`` 读取字节，既可校验边界，也能将传输失败明确返回。
        """
        output_root = source.resolve()
        target_root = PurePosixPath(target)
        status, paths_raw, stderr = await self._run_cli(
            "exec", "--user", f"{settings.AGENT_SANDBOX_DOCKER_UID}:{settings.AGENT_SANDBOX_DOCKER_GID}",
            container_id, "/bin/sh", "-c", 'find "$1" -type f -print0', "--", target,
            timeout=10,
        )
        if status != 0:
            raise RuntimeError(self._clean_detail(stderr) or "枚举沙箱产物失败")
        paths = [item for item in paths_raw.split(b"\0") if item]
        max_files = max(1, int(settings.AGENT_SANDBOX_MAX_ARTIFACT_FILES))
        if len(paths) > max_files:
            raise RuntimeError(f"沙箱产物数量超过限制（>{max_files}）")

        total_bytes = 0
        max_bytes = max(1, int(settings.AGENT_SANDBOX_MAX_ARTIFACT_BYTES))
        for raw_path in paths:
            try:
                container_path = PurePosixPath(raw_path.decode("utf-8", errors="strict"))
            except UnicodeDecodeError as exc:
                raise RuntimeError("沙箱产物路径不是有效 UTF-8") from exc
            if (
                not container_path.is_absolute()
                or ".." in container_path.parts
                or not container_path.is_relative_to(target_root)
            ):
                raise RuntimeError("沙箱返回了越界产物路径")
            relative = container_path.relative_to(target_root)
            if not relative.parts or ".." in relative.parts:
                raise RuntimeError("沙箱返回了无效产物路径")
            destination = (output_root / Path(*relative.parts)).resolve()
            if not destination.is_relative_to(output_root):
                raise RuntimeError("沙箱产物目标越过授权目录")
            status, payload, stderr = await self._run_cli(
                "exec", "--user", f"{settings.AGENT_SANDBOX_DOCKER_UID}:{settings.AGENT_SANDBOX_DOCKER_GID}",
                container_id, "cat", str(container_path), timeout=10,
            )
            if status != 0:
                raise RuntimeError(self._clean_detail(stderr) or f"读取沙箱产物失败：{relative.as_posix()}")
            total_bytes += len(payload)
            if total_bytes > max_bytes:
                raise RuntimeError(f"沙箱产物总大小超过限制（>{max_bytes} bytes）")
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(destination.write_bytes, payload)

    async def run_script(
        self,
        code: str,
        language: str = "python",
        timeout: int = 30,
        env_extra: dict[str, str] | None = None,
        mounts: list[dict[str, str]] | None = None,
    ) -> SandboxResult:
        if language != "python":
            return SandboxResult(status="rejected", error="Docker 沙箱仅支持 Python", resource_usage={"sandbox": self.name})
        if len(code) > settings.AGENT_SANDBOX_MAX_CODE_CHARS:
            return SandboxResult(status="rejected", error="脚本代码超过安全长度限制", resource_usage={"sandbox": self.name})
        available, reason = self.is_available()
        if not available:
            return SandboxResult(status="rejected", error=reason, resource_usage={"sandbox": self.name})
        try:
            transfers = [self._validated_transfer(item) for item in (mounts or [])]
        except (TypeError, ValueError) as exc:
            return SandboxResult(status="rejected", error=f"沙箱文件授权无效：{exc}", resource_usage={"sandbox": self.name})

        timeout = max(1, min(int(timeout), settings.AGENT_SANDBOX_TIMEOUT_SECONDS))
        writable_targets = [target for _, target, writable in transfers if writable]
        cmd = [
            "create", "-i",
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", str(settings.AGENT_SANDBOX_DOCKER_PIDS_LIMIT),
            "--memory", settings.AGENT_SANDBOX_DOCKER_MEMORY,
            "--memory-swap", settings.AGENT_SANDBOX_DOCKER_MEMORY,
            "--cpus", str(settings.AGENT_SANDBOX_DOCKER_CPUS),
            "--ulimit", "nofile=64:64",
            "--user", f"{settings.AGENT_SANDBOX_DOCKER_UID}:{settings.AGENT_SANDBOX_DOCKER_GID}",
            "--workdir", "/workspace",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs", "/workspace:rw,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=700",
        ]
        for key, value in (env_extra or {}).items():
            if key.startswith("LUMI_") and isinstance(value, str):
                cmd.extend(["--env", f"{key}={value}"])
        # 容器主进程只负责保持存活。输入随后经固定 exec 命令写进 /workspace
        # tmpfs，真正脚本同样通过 exec 启动，rootfs 仍始终保持只读。
        cmd.extend([
            settings.AGENT_SANDBOX_DOCKER_IMAGE,
            "python", "-I", "-c", f"import time; time.sleep({timeout + 15})",
        ])
        started = time.monotonic()
        container_id = ""
        outcome: SandboxResult | None = None
        copy_errors: list[str] = []
        try:
            status, out, err = await self._run_cli(*cmd, timeout=10)
            if status != 0:
                return SandboxResult(status="error", error=f"创建 Docker 沙箱失败：{self._clean_detail(err)}", resource_usage={"sandbox": self.name})
            container_id = out.decode("utf-8", errors="replace").strip()
            if not container_id:
                return SandboxResult(status="error", error="Docker 沙箱未返回容器标识", resource_usage={"sandbox": self.name})
            status, _, err = await self._run_cli("start", container_id, timeout=10)
            if status != 0:
                return SandboxResult(status="error", error=f"启动 Docker 沙箱失败：{self._clean_detail(err)}", resource_usage={"sandbox": self.name})
            for source, target, writable in transfers:
                if writable:
                    continue
                status, _, err = await self._copy_input(container_id, source, target)
                if status != 0:
                    return SandboxResult(status="error", error=f"准备沙箱输入失败：{self._clean_detail(err)}", resource_usage={"sandbox": self.name})
            status, _, err = await self._prepare_output_dirs(container_id, writable_targets)
            if status != 0:
                return SandboxResult(status="error", error=f"准备沙箱输出失败：{self._clean_detail(err)}", resource_usage={"sandbox": self.name})
            status, stdout, stderr = await self._run_cli(
                "exec", "-i", "--workdir", "/workspace",
                "--user", f"{settings.AGENT_SANDBOX_DOCKER_UID}:{settings.AGENT_SANDBOX_DOCKER_GID}",
                container_id, "python", "-I", "-",
                stdin=code.encode("utf-8"), timeout=timeout + 5,
            )
            max_chars = settings.AGENT_SANDBOX_MAX_OUTPUT_CHARS
            outcome = SandboxResult(
                status="success" if status == 0 else "error",
                stdout=stdout.decode("utf-8", errors="replace")[:max_chars],
                stderr=stderr.decode("utf-8", errors="replace")[:max_chars],
                error=None if status == 0 else (self._clean_detail(stderr) or f"退出码 {status}"),
                duration_ms=int((time.monotonic() - started) * 1000),
                resource_usage={"sandbox": self.name, "exit_code": status, "truncated": len(stdout) > max_chars or len(stderr) > max_chars},
            )
            return outcome
        except asyncio.TimeoutError:
            return SandboxResult(status="timeout", error=f"代码执行超时（>{timeout}s）", duration_ms=int((time.monotonic() - started) * 1000), resource_usage={"sandbox": self.name})
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(status="error", error=f"启动 Docker 沙箱失败：{type(exc).__name__}", duration_ms=int((time.monotonic() - started) * 1000), resource_usage={"sandbox": self.name})
        finally:
            if container_id:
                for source, target, writable in transfers:
                    if writable:
                        try:
                            await self._copy_output_tree(container_id, source, target)
                        except Exception as exc:  # noqa: BLE001
                            copy_errors.append(str(exc) or type(exc).__name__)
                if copy_errors and outcome is not None:
                    # 产物没有可靠回传到用户隔离目录，不能把脚本退出码 0 误报为成功。
                    outcome.status = "error"
                    outcome.error = f"沙箱产物回传失败：{'; '.join(copy_errors[:2])}"
                    outcome.stderr = (outcome.stderr + "\n" + outcome.error).strip()
                try:
                    await self._run_cli("rm", "-f", container_id, timeout=10)
                except Exception:  # noqa: BLE001
                    pass

    async def run_command(self, cmd: list[str], timeout: int = 30) -> SandboxResult:
        return SandboxResult(status="rejected", error="Docker 沙箱不开放任意命令执行", resource_usage={"sandbox": self.name})

    async def close(self) -> None:
        return None
