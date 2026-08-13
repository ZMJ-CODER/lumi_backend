"""Temporal 运行管理 —— 让 Worker 随后端进程一起启动（开发/IDE 模式）.

在 PyCharm 里点“运行后端” = API + Temporal Worker 同时就绪；
Temporal 开发服务器未启动时还可自动拉起（tools/temporal/temporal.exe）。

生产/容器部署可关闭（TEMPORAL_RUN_WORKER_INPROCESS=False），
改用独立 Worker 进程：python -m app.agents.orchestration.temporal.worker。
"""

import asyncio
import subprocess
from pathlib import Path

from loguru import logger

from app.core.config import PROJECT_ROOT, settings

_worker_task: asyncio.Task | None = None


def _temporal_host_port() -> tuple[str, int]:
    """从 TEMPORAL_ADDRESS（host:port）解析主机与端口."""
    addr = settings.TEMPORAL_ADDRESS
    host, _, port = addr.rpartition(":")
    return (host or "127.0.0.1"), int(port)


async def _server_ready(timeout: float = 2.0) -> bool:
    """TCP 探测 Temporal 前端 gRPC 端口是否可连."""
    host, port = _temporal_host_port()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:  # noqa: BLE001
        return False


async def ensure_temporal_server() -> bool:
    """确保 Temporal 开发服务器可连；不可连且允许时自动拉起."""
    if await _server_ready():
        return True
    if not settings.TEMPORAL_AUTO_START_SERVER:
        logger.warning("Temporal 服务器不可达（{}），未开启自动启动", settings.TEMPORAL_ADDRESS)
        return False

    exe = Path(PROJECT_ROOT) / "tools" / "temporal" / "temporal.exe"
    if not exe.exists():
        logger.warning("未找到 Temporal CLI（{}），跳过自动启动", exe)
        return False

    logger.info("自动启动 Temporal 开发服务器: {}", exe)
    log_dir = exe.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    out_f = (log_dir / "temporal-dev.out.log").open("ab")
    err_f = (log_dir / "temporal-dev.err.log").open("ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [
                str(exe),
                "server",
                "start-dev",
                "--namespace",
                settings.TEMPORAL_NAMESPACE,
                "--db-filename",
                "temporal.db",
            ],
            cwd=str(exe.parent),
            stdout=out_f,
            stderr=err_f,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Temporal 服务器自动启动失败: {}", exc)
        return False
    finally:
        out_f.close()
        err_f.close()

    for _ in range(30):
        if await _server_ready():
            logger.info("Temporal 开发服务器已就绪")
            return True
        await asyncio.sleep(1)
    logger.warning("Temporal 服务器启动超时，请手动运行: {} server start-dev", exe)
    return False


async def start_inprocess_worker() -> None:
    """在 API 进程内启动 Temporal Worker（幂等）."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    if not await ensure_temporal_server():
        return  # 服务器不可用：编排器会回退 legacy 自建 DAG

    from temporalio.client import Client

    from app.agents.orchestration.temporal.worker import build_worker
    from app.agents.skills.registry import SkillRegistry, init_skills
    from app.core import redis as redis_mod

    if redis_mod.redis_client is None:
        await redis_mod.init_redis()
    if not SkillRegistry.list():
        init_skills()

    client = await Client.connect(
        settings.TEMPORAL_ADDRESS, namespace=settings.TEMPORAL_NAMESPACE
    )
    worker = build_worker(client)
    logger.info(
        "Temporal Worker 已随后端进程启动: {} queue={}",
        settings.TEMPORAL_ADDRESS,
        settings.TEMPORAL_TASK_QUEUE,
    )
    _worker_task = asyncio.create_task(worker.run())
    _worker_task.add_done_callback(_on_worker_done)


def _on_worker_done(task: asyncio.Task) -> None:
    global _worker_task
    if task is _worker_task:
        _worker_task = None
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Temporal Worker 异常退出: {}", exc)


async def stop_inprocess_worker() -> None:
    """停止进程内 Worker（后端关闭时调用）."""
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    logger.info("Temporal Worker 已随后端停止")
