"""``ExecutionResult[T]``：泛型执行结果信封。

设计要点（与方案一致）：

* **只保存统一控制信息**：status / schema / trace / error / retryable / timing /
  artifact_refs / sensitivity；业务字段全部在 ``payload`` 里；
* 业务代码直接 ``result.payload.total_hits``，**不引入多层 canonical_data**；
* 可选 ``output_schema``：给了就校验并产出规范化 payload；校验失败收敛为
  ``INVALID_OUTPUT`` 错误信封，绝不把非法结构放行到下游；
* 不设计万能 BaseMessage：请求、结果、事件各自独立类型。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from lumi_contracts.common.errors import ContractErrorCode, ErrorEnvelope
from lumi_contracts.common.status import ExecutionStatus
from lumi_contracts.common.version import ContractVersion, contract_version
from lumi_contracts.execution.artifacts import ArtifactRef, artifact_refs_from

T = TypeVar("T")


class ExecutionTiming(BaseModel):
    """耗时统计（毫秒）；缺省 0 表示未采集，不表示"瞬间完成"。"""

    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    queue_ms: int = 0


class ExecutionResult(BaseModel, Generic[T]):
    """一次工具/Skill/节点执行的统一结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: ExecutionStatus = ExecutionStatus.SUCCESS
    # 业务字段的唯一载体。默认不限类型；注册了 output_schema 时会被规范化。
    payload: T | None = None

    # ── 契约标识 ──
    schema_name: str = ""
    schema_version: int = 1
    # 结果来自哪个工具/Skill（限定名或原始名）；用于投影与审计。
    tool_name: str = ""
    namespace: str = ""

    # ── 关联标识（由服务端注入，插件不得伪造）──
    trace_id: str = ""
    request_id: str = ""
    call_id: str = ""
    job_id: str = ""
    node_id: str = ""

    # ── 控制面 ──
    error: ErrorEnvelope | None = None
    retryable: bool = False
    partial: bool = False
    timing: ExecutionTiming = Field(default_factory=ExecutionTiming)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    sensitivity: str = ""

    # ── 兼容观察字段（迁移期：旧调用方仍读 output）──
    output: str = ""
    content_type: str = "text"
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ── 派生 ──────────────────────────────────────────────

    @property
    def ok(self) -> bool:
        return self.status.is_ok

    @property
    def contract(self) -> ContractVersion:
        name = self.schema_name or "execution_result"
        return contract_version(name, self.schema_version)

    def require_payload(self) -> T:
        """取业务 payload；为空时抛 ``ContractError``（调用方需显式处理）。"""
        if self.payload is None:
            from lumi_contracts.common.errors import ContractError

            raise ContractError(
                ContractErrorCode.INVALID_OUTPUT,
                f"{self.tool_name or 'tool'} 返回了空 payload",
                details={"schema": str(self.contract)},
            )
        return self.payload

    def with_contract(self, name: str, version: int = 1) -> "ExecutionResult[T]":
        return self.model_copy(update={"schema_name": name, "schema_version": int(version)})

    def to_dict(self) -> dict[str, Any]:
        """跨进程序列化（MCP/DAG/SSE 边界统一使用它）。"""
        return self.model_dump(mode="json", exclude_none=True)


def ok(
    payload: T | None,
    *,
    tool_name: str = "",
    schema_name: str = "",
    schema_version: int = 1,
    call_id: str = "",
    content_type: str = "text",
    output: str = "",
    metadata: dict[str, Any] | None = None,
    artifact_refs: object = None,
    sensitivity: str = "",
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
) -> ExecutionResult[T]:
    """构造成功结果（``partial``/``empty`` 通过 ``status`` 指定）。"""
    return ExecutionResult[T](
        status=status,
        payload=payload,
        tool_name=tool_name,
        schema_name=schema_name,
        schema_version=int(schema_version),
        call_id=call_id,
        content_type=content_type,
        output=output,
        metadata=dict(metadata or {}),
        artifact_refs=artifact_refs_from(artifact_refs),
        sensitivity=sensitivity,
    )


def failure(
    code: ContractErrorCode | str,
    message: str = "",
    *,
    tool_name: str = "",
    retryable: bool = False,
    suggested_action: str = "",
    details: dict[str, Any] | None = None,
    status: ExecutionStatus = ExecutionStatus.FAILED,
) -> ExecutionResult[Any]:
    """构造失败结果（错误码稳定，禁止用自由文本当错误码）。"""
    return ExecutionResult[Any](
        status=status,
        tool_name=tool_name,
        retryable=bool(retryable),
        error=ErrorEnvelope(
            code=str(code),
            message=str(message or code),
            retryable=bool(retryable),
            suggested_action=str(suggested_action or ""),
            details=dict(details or {}),
        ),
    )


__all__ = [
    "ExecutionResult",
    "ExecutionTiming",
    "failure",
    "ok",
]
