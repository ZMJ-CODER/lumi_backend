"""可序列化的监控事件数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from app.monitoring.context import MonitorContext


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    result: dict[str, Any] = {}
    blocked = ("api_key", "token", "password", "secret", "prompt", "content", "body")
    for key, value in metadata.items():
        key_text = str(key).lower()
        if any(word in key_text for word in blocked):
            result[str(key)] = "[redacted]"
        elif isinstance(value, str):
            result[str(key)] = value[:500]
        else:
            result[str(key)] = value
    return result


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    event_type: str
    category: str
    severity: str
    code: str
    message: str
    context: MonitorContext = field(default_factory=MonitorContext)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type[:80],
            "category": self.category[:80],
            "severity": self.severity[:20],
            "code": self.code[:100],
            "message": self.message[:1000],
            "timestamp": self.timestamp,
            **self.context.sanitized(),
            "metadata": _safe_metadata(self.metadata),
        }
