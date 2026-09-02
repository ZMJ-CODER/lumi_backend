"""中立执行遥测端口的 Lumi 适配器。"""

from __future__ import annotations

from lumi_execution import NodeExecutionMetrics


class LumiExecutionTelemetry:
    async def record(self, metrics: NodeExecutionMetrics) -> None:
        try:
            from app.core.observability import observe_agent_node_duration

            agent = str(metrics.attributes.get("agent") or "unknown")
            observe_agent_node_duration(
                agent,
                bool(metrics.attributes.get("success", False)),
                metrics.execution_seconds,
            )
        except Exception:  # noqa: BLE001
            return None
