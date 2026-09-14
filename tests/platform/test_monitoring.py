from app.monitoring.context import MonitorContext
from app.monitoring.events import MonitorEvent
from app.monitoring.exceptions import TimeoutMonitorError
from app.monitoring.logger import MonitorLogger


def test_monitor_context_and_event_redact_sensitive_fields():
    event = MonitorEvent(
        event_type="node_failed",
        category="execution",
        severity="ERROR",
        code="NODE_FAILED",
        message="node failed",
        context=MonitorContext(job_id="job-1", user_id="user-1"),
        metadata={"api_key": "secret", "prompt": "private", "attempt": 2},
    )
    value = event.to_dict()
    assert value["job_id"] == "job-1"
    assert value["metadata"] == {"api_key": "[redacted]", "prompt": "[redacted]", "attempt": 2}


def test_monitor_logger_is_best_effort_and_exception_has_contract():
    MonitorLogger().record(
        MonitorEvent("test", "system", "INFO", "TEST", "ok")
    )
    exc = TimeoutMonitorError("slow")
    assert exc.code == "EXECUTION_TIMEOUT"
    assert exc.category == "timeout"
    assert exc.retryable

