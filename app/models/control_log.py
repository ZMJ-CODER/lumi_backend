"""操控日志模块数据模型."""

from pydantic import BaseModel, Field


class LogEntry(BaseModel):
    action: str
    target: str
    success: bool
    timestamp: str


class BatchUploadLogsRequest(BaseModel):
    logs: list[LogEntry]
