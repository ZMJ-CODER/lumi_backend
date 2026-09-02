"""进程内资源类别的调度原语。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class ResourceDispatcher:
    """Separate IO, CPU and external-dependency concurrency windows."""

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        values = limits or {}
        self._semaphores = {
            name: asyncio.Semaphore(max(1, int(values.get(name, 1))))
            for name in ("io_bound", "cpu_bound", "external_dependency")
        }

    @asynccontextmanager
    async def claim(self, resource_class: str) -> AsyncIterator[None]:
        semaphore = self._semaphores.get(resource_class, self._semaphores["io_bound"])
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()
