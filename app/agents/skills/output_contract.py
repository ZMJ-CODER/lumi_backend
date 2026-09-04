"""技能输出契约：把工具原始结果与模型可见结果明确分层。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ArtifactRef(BaseModel):
    """后端产物引用；不允许把宿主路径或凭据暴露给模型。"""

    ref_id: str
    name: str = ""
    media_type: str = "application/octet-stream"
    size: int | None = None


class Citation(BaseModel):
    """可展示的来源定位信息，正文只保留短摘录。"""

    title: str = ""
    source: str = ""
    snippet: str = ""
    locator: str = ""


class OutputMeta(BaseModel):
    """工具输出的机器元数据，不承担模型提示词拼接。"""

    total_size: int = 0
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    summary: str = ""
    quality_hints: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict[str, Any]] = Field(default_factory=list)


class ToolOutput(BaseModel):
    """工具、工作流、MCP 与 DAG 共用的唯一执行结果信封。

    ``status/data/content_type/meta`` 是跨进程、跨边界的正式契约。其余旧字段
    仅允许作为插件迁移期间的构造输入；网关和调度层必须使用
    :meth:`to_execution_envelope` 传递本对象，不能重新拼装 ``content``、
    ``metadata`` 等散字段。
    """

    status: Literal["success", "partial", "failed", "empty", "pending", "pending_approval", "uncertain"] = "success"
    data: Any = None
    content_type: Literal["text", "structured", "artifact", "streaming"] = "text"
    meta: OutputMeta = Field(default_factory=OutputMeta)
    # 兼容旧调用方的观察字段；新插件应直接填 status/data/meta。
    success: bool | None = None
    output: str = ""
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    output_meta: OutputMeta | None = None

    def model_post_init(self, __context: Any) -> None:
        """把旧字段转换为新信封，保证迁移期间输入输出一致。"""
        supplied_success = self.success
        if supplied_success is None:
            self.success = self.status in {"success", "partial", "empty"}
        else:
            self.status = "success" if self.success else "failed"
        if supplied_success is not None and self.success and self.metadata.get("partial"):
            self.status = "partial"
        if self.success and not self.data:
            self.data = self.output
            if not self.data:
                self.status = "empty"
        if self.success is False and self.data is None:
            self.data = self.error or "执行失败"
        if self.metadata:
            legacy_hints = dict(self.metadata.get("quality_hints") or {})
            if isinstance(self.metadata.get("decision_signals"), dict):
                legacy_hints["decision_signals"] = dict(self.metadata["decision_signals"])
            self.meta = OutputMeta(
                total_size=int(self.metadata.get("total_size") or len(self.output or "")),
                artifact_refs=self.meta.artifact_refs,
                summary=str(self.metadata.get("summary") or self.meta.summary),
                quality_hints={**self.meta.quality_hints, **legacy_hints},
                citations=[dict(item) for item in self.metadata.get("citations", self.meta.citations) if isinstance(item, dict)],
            )
            if self.success and self.metadata.get("partial"):
                self.status = "partial"
        if self.output_meta is not None:
            if isinstance(self.output_meta, dict):
                self.output_meta = OutputMeta.model_validate(self.output_meta)
            self.meta = self.output_meta
        if self.success is None and self.status == "success" and self.data is None:
            self.status = "empty"
        if not self.output and self.content_type == "text" and isinstance(self.data, str):
            self.output = self.data

    def decision_signals(self) -> dict[str, Any]:
        signals = self.metadata.get("decision_signals") if isinstance(self.metadata, dict) else None
        if isinstance(signals, dict):
            return dict(signals)
        signals = self.meta.quality_hints.get("decision_signals") if self.meta else None
        if isinstance(signals, dict):
            return dict(signals)
        return {"result_count": self.meta.quality_hints.get("result_count"), "more_available": False, "truncated": False}

    def to_tool_output(self) -> "ToolOutput":
        return self

    def to_execution_envelope(self) -> dict[str, Any]:
        """Serialize the stable execution contract for MCP/DAG/SSE boundaries."""
        return {
            "status": self.status,
            "data": self.data,
            "content_type": self.content_type,
            "meta": self.meta.model_dump(mode="json", exclude_none=True),
            "error": self.error,
            "error_code": self.error_code,
            "retryable": self.retryable,
        }
