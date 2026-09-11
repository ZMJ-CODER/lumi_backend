"""遗留结果适配层：把旧 ``ToolOutput`` / MCP 裸字典 / 第三方对象转成 ``ExecutionResult``。

铁律（与方案第六、八节一致）：

* **裸字典只允许存在于本模块内部**，转换后不得继续向下游扩散；
* 无法无损转换时抛 ``UnsupportedContractVersion``（错误码
  ``UNSUPPORTED_CONTRACT_VERSION``），**不静默丢字段**；
* 不把 reasoning / 提示词残留拼进正文；
* 旧 ``ToolOutput`` 的字段语义在这里一次性映射完成，业务层不再各自判断。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lumi_contracts.common.errors import ContractError, ContractErrorCode
from lumi_contracts.common.status import ExecutionStatus
from lumi_contracts.execution.artifacts import ArtifactRef, artifact_refs_from
from lumi_contracts.execution.result import ExecutionResult, ExecutionTiming


class UnsupportedContractVersion(ContractError):
    """契约版本无法转换（不静默降级）。"""

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(
            ContractErrorCode.UNSUPPORTED_CONTRACT_VERSION,
            message,
            retryable=False,
            details=details,
        )


# 旧 ToolOutput.content_type → 新契约的观察字段（保持同名，避免下游再判断）。
_CONTENT_TYPES = frozenset({"text", "structured", "artifact", "streaming"})


class LegacyEnvelopeAdapter:
    """把一个遗留结果对象/字典归一为 ``ExecutionResult``。"""

    def __init__(self, *, tool_name: str = "", namespace: str = "") -> None:
        self._tool_name = str(tool_name or "")
        self._namespace = str(namespace or "")

    # ── 入口 ──────────────────────────────────────────────

    def adapt(self, value: Any) -> ExecutionResult[Any]:
        if isinstance(value, ExecutionResult):
            return value
        if value is None:
            return ExecutionResult[Any](
                status=ExecutionStatus.EMPTY,
                tool_name=self._tool_name,
                namespace=self._namespace,
            )
        # 旧 ToolOutput（pydantic）或任何具备同名属性的对象
        if hasattr(value, "to_execution_envelope") or hasattr(value, "status"):
            return self._from_legacy_output(value)
        if isinstance(value, dict):
            if "payload" in value and "status" in value:
                # 已经是新契约的 dict 形态
                return self._from_contract_dict(value)
            return self._from_legacy_dict(value)
        # 第三方结果对象：按属性名保守提取
        return self._from_legacy_output(value)

    def adapt_envelope(self, envelope: dict) -> ExecutionResult[Any]:
        """适配 ``to_execution_envelope()`` 产出的执行信封（MCP hop 之后的形态）。

        注意：契约形态 dict（``{status, payload, schema_name...}``）也走这里，
        因此委托给 :meth:`adapt` 统一识别，避免 ``payload`` 被当成缺失。
        """
        if not isinstance(envelope, dict):
            raise UnsupportedContractVersion(
                "执行信封必须是对象", details={"actual": type(envelope).__name__}
            )
        return self.adapt(envelope)

    # ── 内部 ──────────────────────────────────────────────

    def _from_contract_dict(self, value: dict) -> ExecutionResult[Any]:
        status = ExecutionStatus.coerce(value.get("status"))
        error_raw = value.get("error")
        error = None
        if isinstance(error_raw, dict):
            from lumi_contracts.common.errors import ErrorEnvelope

            error = ErrorEnvelope.model_validate(error_raw)
        elif isinstance(error_raw, str) and error_raw:
            from lumi_contracts.common.errors import ErrorEnvelope

            error = ErrorEnvelope(code=str(value.get("error_code") or "UNKNOWN"), message=error_raw)
        return ExecutionResult[Any](
            status=status,
            payload=value.get("payload", value.get("data")),
            schema_name=str(value.get("schema_name") or ""),
            schema_version=int(value.get("schema_version") or 1),
            tool_name=str(value.get("tool_name") or self._tool_name),
            namespace=str(value.get("namespace") or self._namespace),
            trace_id=str(value.get("trace_id") or ""),
            request_id=str(value.get("request_id") or ""),
            call_id=str(value.get("call_id") or ""),
            job_id=str(value.get("job_id") or ""),
            node_id=str(value.get("node_id") or ""),
            error=error,
            retryable=bool(value.get("retryable", False)),
            partial=status is ExecutionStatus.PARTIAL,
            artifact_refs=artifact_refs_from(value.get("artifact_refs") or value.get("artifacts")),
            sensitivity=str(value.get("sensitivity") or ""),
            output=str(value.get("output") or ""),
            content_type=str(value.get("content_type") or "text"),
            metadata=dict(value.get("metadata") or {}),
        )

    def _from_legacy_transport_dict(self, value: dict) -> ExecutionResult[Any]:
        """旧传输形态：``{success, content, data, metadata, is_error, error}``。

        第三方 MCP 实现仍在返回这一形态。它的语义与旧 ``ToolOutput`` 的
        ``success/content/metadata`` 完全一致，因此在这里一次性映射完成，
        业务层不需要再判断"这个 dict 是哪种信封"。
        """
        from lumi_contracts.common.errors import ErrorEnvelope

        is_error = bool(value.get("is_error"))
        success = bool(value.get("success")) and not is_error
        raw_metadata = value.get("metadata")
        metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        output = str(value.get("content") or value.get("output") or "")
        payload = value.get("data")
        if payload is None:
            payload = output
        content_type = str(value.get("content_type") or metadata.get("content_type") or "text")
        if content_type not in _CONTENT_TYPES:
            content_type = "text"
        error_code = str(value.get("error_code") or "")
        error_message = str(value.get("error") or (output if is_error else "") or "")
        status = ExecutionStatus.SUCCESS if success else ExecutionStatus.FAILED
        # 高风险工具的"待确认"不是失败：保留明确状态供审批链路消费。
        if not success and error_code == "NEEDS_CONFIRMATION":
            status = ExecutionStatus.PENDING_APPROVAL
        if not success:
            payload = error_message or "执行失败"
        elif not payload:
            status = ExecutionStatus.EMPTY
        elif bool(metadata.get("partial")):
            status = ExecutionStatus.PARTIAL
        error = None
        if not status.is_ok and (error_code or error_message):
            error = ErrorEnvelope(
                code=error_code,
                message=error_message,
                retryable=bool(value.get("retryable", False)),
            )
        # 旧 ``metadata`` 就是元数据通道；放进 ``metadata["meta"]`` 以便回程
        # 适配（``to_tool_output``）还原成同一个 OutputMeta。
        carried: dict[str, Any] = {"meta": metadata}
        if metadata.get("quality_hints"):
            carried["quality_hints"] = dict(metadata["quality_hints"])
        if metadata.get("decision_signals"):
            carried["decision_signals"] = dict(metadata["decision_signals"])
        return ExecutionResult[Any](
            status=status,
            payload=payload,
            tool_name=self._tool_name,
            namespace=self._namespace,
            call_id=str(value.get("call_id") or ""),
            error=error,
            retryable=bool(value.get("retryable", False)),
            partial=status is ExecutionStatus.PARTIAL,
            artifact_refs=artifact_refs_from(metadata.get("artifact_refs")),
            output=output,
            content_type=content_type,
            metadata=carried,
        )

    def _from_legacy_output(self, value: Any) -> ExecutionResult[Any]:
        status = ExecutionStatus.coerce(getattr(value, "status", None))
        content_type = str(getattr(value, "content_type", "") or "text")
        if content_type not in _CONTENT_TYPES:
            raise UnsupportedContractVersion(
                f"未知 content_type：{content_type}",
                details={"tool": str(getattr(value, "name", "") or self._tool_name)},
            )
        meta = getattr(value, "meta", None)
        artifact_refs = artifact_refs_from(getattr(meta, "artifact_refs", None)) if meta else []
        summary = str(getattr(meta, "summary", "") or "") if meta else ""
        metadata: dict[str, Any] = dict(getattr(value, "metadata", None) or {})
        if summary:
            metadata.setdefault("summary", summary)
        quality_hints = getattr(meta, "quality_hints", None) if meta else None
        if isinstance(quality_hints, dict) and quality_hints:
            metadata.setdefault("quality_hints", dict(quality_hints))
        error_code = getattr(value, "error_code", None)
        error_message = getattr(value, "error", None)
        error = None
        if not status.is_ok and (error_code or error_message):
            from lumi_contracts.common.errors import ErrorEnvelope

            error = ErrorEnvelope(
                code=str(error_code or ""),
                message=str(error_message or ""),
                retryable=bool(getattr(value, "retryable", False)),
            )
        return ExecutionResult[Any](
            status=status,
            payload=getattr(value, "data", None),
            tool_name=self._tool_name,
            namespace=self._namespace,
            call_id=str(getattr(value, "call_id", "") or ""),
            error=error,
            retryable=bool(getattr(value, "retryable", False)),
            partial=status is ExecutionStatus.PARTIAL,
            artifact_refs=artifact_refs,
            output=str(getattr(value, "output", "") or ""),
            content_type=content_type,
            metadata=metadata,
        )

    def _from_legacy_dict(self, value: dict) -> ExecutionResult[Any]:
        """MCP/Electron 信封：{call_id,status,data,content_type,meta,artifacts,error,error_code}。"""
        if not value:
            return ExecutionResult[Any](
                status=ExecutionStatus.EMPTY,
                tool_name=self._tool_name,
                namespace=self._namespace,
            )
        if "status" not in value and ("success" in value or "is_error" in value or "content" in value):
            # 第三方 MCP 实现仍在用的旧传输形态：
            # {success, content, data, metadata, is_error, error}。
            return self._from_legacy_transport_dict(value)
        status = ExecutionStatus.coerce(value.get("status"))
        content_type = str(value.get("content_type") or "text")
        if content_type not in _CONTENT_TYPES:
            raise UnsupportedContractVersion(
                f"未知 content_type：{content_type}",
                details={"keys": sorted(value)[:20]},
            )
        meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
        metadata: dict[str, Any] = {}
        if meta:
            metadata["meta"] = dict(meta)
            if meta.get("quality_hints"):
                metadata["quality_hints"] = dict(meta["quality_hints"])
            if meta.get("decision_signals"):
                metadata["decision_signals"] = dict(meta["decision_signals"])
        error_code = value.get("error_code")
        error_message = value.get("error")
        error = None
        if error_code or (error_message and not status.is_ok):
            from lumi_contracts.common.errors import ErrorEnvelope

            error = ErrorEnvelope(
                # 不伪造 "UNKNOWN"：调用边界（如 MCP 网关）需要能区分
                # "上游没给错误码" 与 "上游明确给了 UNKNOWN"。
                code=str(error_code or ""),
                message=str(error_message or ""),
                retryable=bool(value.get("retryable", False)),
            )
        return ExecutionResult[Any](
            status=status,
            payload=value.get("data"),
            tool_name=self._tool_name,
            namespace=self._namespace,
            call_id=str(value.get("call_id") or ""),
            error=error,
            retryable=bool(value.get("retryable", False)),
            partial=status is ExecutionStatus.PARTIAL,
            artifact_refs=artifact_refs_from(value.get("artifacts") or meta.get("artifact_refs")),
            output=str(value.get("output") or ""),
            content_type=content_type,
            metadata=metadata,
        )


def adapt_tool_result(value: Any, *, tool_name: str = "", namespace: str = "") -> ExecutionResult[Any]:
    """便捷函数：把任意遗留结果适配成 ``ExecutionResult``。"""
    return LegacyEnvelopeAdapter(tool_name=tool_name, namespace=namespace).adapt(value)


__all__ = [
    "ArtifactRef",
    "ExecutionTiming",
    "LegacyEnvelopeAdapter",
    "UnsupportedContractVersion",
    "adapt_tool_result",
]
