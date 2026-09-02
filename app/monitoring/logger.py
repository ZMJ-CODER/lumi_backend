"""安全的结构化日志门面。

该门面刻意采用失败开放：监控失败绝不能中断用户任务。Loguru 仍是实际输出端，
因此既有部署日志不变；后续适配器可额外持久化事件或发送 Sentry。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.monitoring.events import MonitorEvent


class MonitorLogger:
    def record(self, event: MonitorEvent, *, exc_info: BaseException | None = None) -> None:
        try:
            payload = event.to_dict()
            level = event.severity.upper() if event.severity.upper() in {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"
            bound = logger.bind(**payload)
            if exc_info is not None:
                bound.opt(exception=exc_info).log(level, "[{}] {}", event.code, event.message)
            else:
                bound.log(level, "[{}] {}", event.code, event.message)
        except Exception:
            # Observability is intentionally best effort.
            return

    def log(self, severity: str, message: str, *, event_type: str = "log", category: str = "system", code: str = "LOG", context=None, metadata: dict[str, Any] | None = None) -> None:
        self.record(MonitorEvent(event_type, category, severity, code, message, context or _empty_context(), metadata or {}))

    def debug(self, message: str, **kwargs: Any) -> None:
        self.log("DEBUG", message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self.log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self.log("WARNING", message, **kwargs)

    def error(self, message: str, *, exc_info: BaseException | None = None, **kwargs: Any) -> None:
        event = MonitorEvent(kwargs.pop("event_type", "error"), kwargs.pop("category", "system"), "ERROR", kwargs.pop("code", "ERROR"), message, kwargs.pop("context", _empty_context()), kwargs.pop("metadata", {}))
        self.record(event, exc_info=exc_info)

    def exception(self, message: str, *, exc_info: BaseException | None = None, **kwargs: Any) -> None:
        self.error(message, exc_info=exc_info, **kwargs)


def _empty_context():
    from app.monitoring.context import MonitorContext
    return MonitorContext()


monitor_logger = MonitorLogger()
