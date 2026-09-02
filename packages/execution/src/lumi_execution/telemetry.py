"""运行时无关的节点执行遥测端口。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class NodeExecutionMetrics:
    node_id: str
    queue_seconds: float = 0.0
    execution_seconds: float = 0.0
    retries: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)


class TelemetryPort(Protocol):
    async def record(self, metrics: NodeExecutionMetrics) -> None: ...


class NullTelemetry:
    async def record(self, metrics: NodeExecutionMetrics) -> None:
        return None


class ExecutionTimer:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    def elapsed(self) -> float:
        return max(0.0, time.perf_counter() - self.started)
