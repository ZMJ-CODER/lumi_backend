"""由应用层实现的稳定执行端口。"""

from __future__ import annotations

from typing import Any, Protocol


class JobStateStorePort(Protocol):
    async def create_job(self, job: Any) -> None: ...

    async def get_job(self, job_id: str) -> Any | None: ...

    async def save_job(self, job: Any) -> None: ...


class NodeWorkerPort(Protocol):
    name: str

    async def execute(self, node: Any, context: Any) -> dict[str, Any]: ...


class ReviewPort(Protocol):
    async def review(self, node: Any, result: dict[str, Any], context: Any) -> Any: ...
