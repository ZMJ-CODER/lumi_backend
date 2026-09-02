"""结构化监控事件的关联上下文。"""

from __future__ import annotations

from dataclasses import dataclass


def _short(value: str | None, limit: int = 96) -> str | None:
    if not value:
        return None
    return value[:limit]


@dataclass(frozen=True, slots=True)
class MonitorContext:
    request_id: str | None = None
    trace_id: str | None = None
    job_id: str | None = None
    execution_id: str | None = None
    user_id: str | None = None
    component: str | None = None
    runtime: str | None = None
    node_id: str | None = None

    def sanitized(self) -> dict[str, str]:
        """Return bounded fields safe to attach to logs and Sentry."""
        values = {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "job_id": self.job_id,
            "execution_id": self.execution_id,
            "user_id": self.user_id,
            "component": self.component,
            "runtime": self.runtime,
            "node_id": self.node_id,
        }
        return {key: _short(value) for key, value in values.items() if value}
