"""计算密集型任务的专用线程池.

背景：OCR / Docling / Embedding / TTS 等重活在 async 代码里用 asyncio.to_thread 执行时，
默认线程池只有 CPU 核数 + 4 个线程。多个用户并发上传大图/长音频时，默认池会被
计算任务占满，把 Web 服务其他后台任务（DB 连接维护、FastAPI 同步依赖等）一起饿死。

这里提供独立的、有界线程池 + 统一入口：计算任务只占用自己的配额，互不干扰。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from app.core.config import settings

# 每个线程同一时刻只跑一个任务；计算任务多为 GPU 推理或 IO 密集解析，
# 4 个线程足够覆盖单人/小团队并发；多 worker 部署时每进程一份。
_compute_pool = ThreadPoolExecutor(
    max_workers=max(2, settings.COMPUTE_THREADS),
    thread_name_prefix="compute",
)


async def run_in_compute(fn, *args, **kwargs):
    """在专用计算线程池中执行同步函数，避免占用默认 asyncio 线程池."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_compute_pool, partial(fn, *args, **kwargs))
