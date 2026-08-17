"""日志配置：loguru 应用日志 + uvicorn 访问日志队列化（避免阻塞事件循环）."""

import logging
import queue
import sys
from logging.handlers import QueueHandler, QueueListener

from loguru import logger

from app.core.config import settings

# ── uvicorn 访问日志异步队列 ──────────────────────────────
# Windows 上 uvicorn 默认把访问日志同步写 stderr（重定向到文件时串行阻塞事件循环）。
# 这里把 uvicorn.access / uvicorn.error 切到 QueueHandler：请求路径只做一次队列 put
# （微秒级），由独立监听线程负责实际 I/O，既保留日志又不拖累请求处理。
_access_queue: queue.Queue | None = None
_access_listener: QueueListener | None = None


def setup_logging() -> None:
    """初始化 loguru 日志."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if settings.DEBUG else "INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )
    logger.add(
        "logs/lumi_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )


def setup_uvicorn_queue_logging() -> None:
    """把 uvicorn 访问/错误日志切换到内存队列 + 后台监听线程（幂等）."""
    global _access_queue, _access_listener
    if _access_listener is not None:
        return
    _access_queue = queue.Queue(maxsize=20000)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    _access_listener = QueueListener(_access_queue, handler, respect_handler_level=True)
    _access_listener.start()
    qh = QueueHandler(_access_queue)
    for name in ("uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = False
        lg.addHandler(qh)
        lg.setLevel(logging.INFO)
    logger.info("uvicorn 访问日志已切换到异步队列（QueueHandler）")
