"""编排运行时后端的共享契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.agents.orchestration.models import Job


class ExecutionBackend(Protocol):
    """Runtime lifecycle contract used by the orchestration facade."""

    name: str

    async def submit(
        self, job: Job, llm_api_key: str | None, llm_config: dict | None = None
    ) -> None:
        """Accept one frozen JobSpec-backed job and start its runtime."""

    async def cancel(self, job: Job | None, keep_completed: bool = True): ...

    async def pause(self, job: Job | None): ...

    async def resume(self, job: Job | None): ...

    async def approve(self, job: Job, node_id: str, approved: bool): ...


@dataclass(frozen=True, slots=True)
class BackendControlResult:
    """Result of a backend control request."""

    job: Job
    handled: bool = True
    release_capacity: bool = False
    error: str | None = None
