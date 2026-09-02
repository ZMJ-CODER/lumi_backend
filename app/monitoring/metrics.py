"""监控指标门面，委托给既有 Prometheus 模块。"""

from __future__ import annotations

from app.core import observability


class MonitorMetrics:
    def agent_job(self, status: str) -> None:
        observability.inc_agent_job(status)

    def execution_failure(self, category: str, code: str) -> None:
        # Keep labels bounded and reuse the existing job counter until a
        # dedicated failure counter is introduced in the metrics schema.
        observability.inc_agent_job(f"failed:{category}:{code}"[:100])

    def state_transition(self, current: str, target: str) -> None:
        # State transitions currently have no public metric.  This hook gives
        # callers a stable API without creating a second metrics registry.
        return None

    def skill_call(self, skill: str, success: bool) -> None:
        observability.inc_skill_call(skill, success)


monitor_metrics = MonitorMetrics()
