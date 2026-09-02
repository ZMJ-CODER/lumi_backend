"""执行引擎使用的运行时后端无关端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class NodeExecutor(Protocol):
    async def execute(self, node: Any, context: Any) -> Mapping[str, Any] | None: ...


class TaskNodeExecutor(Protocol):
    """Application adapter for one frozen node in a complete JobSpec."""

    async def execute_node(
        self,
        spec: Any,
        node: Any,
        dependency_results: Mapping[str, dict],
    ) -> Any: ...


class ExecutionControlPort(Protocol):
    """Durable pause/cancel state supplied by Legacy or Temporal."""

    async def get_status(self, job_id: str) -> str: ...


class NodeLifecyclePort(Protocol):
    """Persistence/telemetry callback for node state transitions."""

    async def on_node_state(
        self,
        spec: Any,
        node: Any,
        phase: str,
        result: Any | None,
    ) -> None: ...


class ReviewPort(Protocol):
    async def review(self, node: Any, result: Mapping[str, Any], context: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class FailureDecision:
    """A serializable decision returned after an execution or review failure."""

    retry_same: bool = False
    try_alternative: bool = False
    category: str = "execution"
    replan_required: bool = False
    user_action_required: bool = False
    recovery: Mapping[str, Any] | None = None
    escalation: Mapping[str, Any] | None = None


class FailurePolicy(Protocol):
    def decide(
        self,
        error_code: str | None,
        error: str | None,
        *,
        retryable: bool,
        effectful: bool,
        alternatives_remaining: bool,
    ) -> FailureDecision: ...


ExceptionClassifier = Callable[[Exception], tuple[str | None, str | None]]
AttemptHook = Callable[[int], Awaitable[None]]
