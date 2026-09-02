"""调用执行引擎的运行时无关数据契约。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from lumi_execution.engine import ExecutionOutcome


class ExecutionRuntimePort(Protocol):
    """A runtime adapter that executes one node through the shared engine."""

    async def run_node(
        self,
        *,
        node: Any,
        context: Any,
        executor: Any,
        review: Any,
        failure_policy: Any,
        timeout_seconds: int,
        max_retries: int,
        effectful: bool = False,
        on_running: Callable[[int], Awaitable[None]] | None = None,
        on_retry: Callable[[int], Awaitable[None]] | None = None,
        classify_exception: Callable[[Exception], tuple[str | None, str | None]] | None = None,
    ) -> ExecutionOutcome: ...


class DirectExecutionRuntime:
    """Default in-process adapter used by Legacy and Temporal Activities."""

    async def run_node(self, **kwargs: Any) -> ExecutionOutcome:
        from lumi_execution.engine import ExecutionEngine

        return await ExecutionEngine(**kwargs).run()
