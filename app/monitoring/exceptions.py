"""可被监控的运行故障异常类型。"""

from __future__ import annotations

from app.agents.orchestration.state_machine.errors import ErrorCategory


class MonitoringError(RuntimeError):
    category = ErrorCategory.UNKNOWN.value
    code = "MONITORING_ERROR"
    severity = "error"
    retryable = False
    replannable = False
    user_message = "操作失败"

    def __init__(self, message: str | None = None, *, details=None) -> None:
        self.details = details
        super().__init__(message or self.user_message)


class PlanningMonitorError(MonitoringError):
    category = ErrorCategory.PLANNING.value
    code = "PLANNING_ERROR"
    user_message = "任务规划失败"


class ExecutionMonitorError(MonitoringError):
    category = ErrorCategory.EXECUTION.value
    code = "EXECUTION_ERROR"
    retryable = True
    user_message = "任务执行失败"


class TimeoutMonitorError(ExecutionMonitorError):
    category = ErrorCategory.TIMEOUT.value
    code = "EXECUTION_TIMEOUT"
    user_message = "任务执行超时"


class ApprovalMonitorError(MonitoringError):
    category = ErrorCategory.APPROVAL.value
    code = "APPROVAL_REQUIRED"
    user_message = "任务需要审批"


class CapacityMonitorError(MonitoringError):
    category = ErrorCategory.CAPACITY.value
    code = "CAPACITY_EXCEEDED"
    retryable = True
    user_message = "当前任务容量已满"


class ExternalServiceMonitorError(MonitoringError):
    category = ErrorCategory.EXTERNAL_SERVICE.value
    code = "EXTERNAL_SERVICE_ERROR"
    retryable = True
    user_message = "外部服务暂时不可用"


class StateConflictMonitorError(MonitoringError):
    category = ErrorCategory.STATE_CONFLICT.value
    code = "STATE_CONFLICT"
    user_message = "任务状态已发生变化，请刷新后重试"
