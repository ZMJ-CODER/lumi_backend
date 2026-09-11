"""``CapabilityResult``：一次能力调用的统一结果（阶段 0 冻结）。

与 ``ExecutionResult`` 的关系：``ExecutionResult`` 是"工具/Skill/节点执行"的通用
信封；``CapabilityResult`` 是**跨 Provider 边界**的结果形状（服务端 ↔ 客户端 +
Broker 的错误语义）。两者都用同一套 ``ExecutionStatus`` / ``ErrorEnvelope`` /
``ArtifactRef``，所以 Provider 结果可以无损转成 ``ExecutionResult`` 供既有投影使用。

硬约束：

* **错误码必须是稳定枚举**（``CapabilityErrorCode``），客户端断线/租约过期/本地拒止
  这些情况不能被压成"工具调用失败"；前端要据此给出可行动提示并决定能否重试；
* **大结果转 Artifact**：``payload`` 只放预算内投影，完整结果走 ``artifact_refs``；
* **敏感度随结果回传**：客户端 Provider 必须声明结果敏感度，服务端据此决定能否进
  模型上下文。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lumi_contracts.common.errors import ErrorEnvelope
from lumi_contracts.common.status import ExecutionStatus
from lumi_contracts.execution.artifacts import ArtifactRef, artifact_refs_from
from lumi_contracts.plugins.capability_invocation import SessionBinding
from lumi_contracts.plugins.vocabulary import CapabilityStatus


class CapabilityErrorCode(StrEnum):
    """能力边界的稳定错误码。

    前四个是**结构性状态**（不是"工具失败"）：客户端离线、能力无人提供、租约过期、
    版本不兼容。它们必须一路透传到前端，不能被吞成通用错误。
    """

    # ── Provider 结构状态 ──
    PROVIDER_OFFLINE = "PROVIDER_OFFLINE"
    #: Provider 健康检查/连续失败导致租约被摘除（客户端可重置隔离后恢复）。
    PROVIDER_UNHEALTHY = "PROVIDER_UNHEALTHY"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    #: 执行前解析失败：没有任何 Provider 声明该能力（前端提示安装/启用 Provider）。
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"
    DEPLOYMENT_NOT_ALLOWED = "DEPLOYMENT_NOT_ALLOWED"
    #: 未知插件类型（Extension Handler 未注册；生产默认拒绝）。
    UNKNOWN_PLUGIN_KIND = "UNKNOWN_PLUGIN_KIND"
    # ── 授权与本地拒止 ──
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    #: 审批令牌/窗口已过期（必须重新走审批，不能重放旧令牌）。
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    #: 服务端策略拒绝（Policy Pack）。
    POLICY_DENIED = "POLICY_DENIED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    #: 客户端本地拒止策略拒绝（本机最终否决权，优先于服务端授权）。
    LOCAL_POLICY_DENIED = "LOCAL_POLICY_DENIED"
    SCOPE_DENIED = "SCOPE_DENIED"
    # ── 输入/输出 ──
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    INVALID_RESULT = "INVALID_RESULT"
    # ── 工作区上下文（由客户端 Provider Runtime 判定；服务端原样透传）──
    # 这些不是"能力失败"而是"本机工作区不可用"：绑定缺失/设备离线/未注册/根目录丢失/
    # 路径不是目录。前端据此给"重新绑定工作区/重连设备"而不是"重试"。
    WORKSPACE_NOT_BOUND = "WORKSPACE_NOT_BOUND"
    WORKSPACE_DEVICE_OFFLINE = "WORKSPACE_DEVICE_OFFLINE"
    WORKSPACE_NOT_REGISTERED = "WORKSPACE_NOT_REGISTERED"
    WORKSPACE_ROOT_MISSING = "WORKSPACE_ROOT_MISSING"
    WORKSPACE_PATH_NOT_DIRECTORY = "WORKSPACE_PATH_NOT_DIRECTORY"
    # ── 运行 ──
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


#: 可由调用方原样重试的错误码（幂等键未变时重试安全）。
#:
#: 与客户端表格对齐：``APPROVAL_REQUIRED`` 也算可重试——补上审批后**同一次调用**可以
#: 重新发起（指纹未变、幂等键未变），不是"换个参数再试"。
RETRYABLE_CAPABILITY_ERRORS: frozenset[str] = frozenset(
    {
        CapabilityErrorCode.PROVIDER_OFFLINE.value,
        CapabilityErrorCode.PROVIDER_UNHEALTHY.value,
        CapabilityErrorCode.LEASE_EXPIRED.value,
        CapabilityErrorCode.TIMEOUT.value,
        CapabilityErrorCode.RESOURCE_EXHAUSTED.value,
        CapabilityErrorCode.APPROVAL_REQUIRED.value,
    }
)

#: 需要用户先补装/启用 Provider 才能继续的错误码（前端据此给"安装/启用"入口）。
INSTALL_REQUIRED_CAPABILITY_ERRORS: frozenset[str] = frozenset(
    {
        CapabilityErrorCode.CAPABILITY_UNAVAILABLE.value,
        CapabilityErrorCode.CAPABILITY_MISSING.value,
        CapabilityErrorCode.CONTRACT_VERSION_MISMATCH.value,
    }
)

#: 能力错误码 → 前端展示状态（投影用；前端只渲染，不自己归类）。
ERROR_TO_CAPABILITY_STATUS: dict[str, CapabilityStatus] = {
    CapabilityErrorCode.PROVIDER_OFFLINE.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.PROVIDER_UNHEALTHY.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.CAPABILITY_UNAVAILABLE.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.CAPABILITY_MISSING.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.LEASE_EXPIRED.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.CONTRACT_VERSION_MISMATCH.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.DEPLOYMENT_NOT_ALLOWED.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.UNKNOWN_PLUGIN_KIND.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.APPROVAL_REQUIRED.value: CapabilityStatus.WAITING_APPROVAL,
    CapabilityErrorCode.APPROVAL_INVALID.value: CapabilityStatus.WAITING_APPROVAL,
    CapabilityErrorCode.APPROVAL_EXPIRED.value: CapabilityStatus.WAITING_APPROVAL,
    CapabilityErrorCode.POLICY_DENIED.value: CapabilityStatus.DENIED,
    CapabilityErrorCode.PERMISSION_DENIED.value: CapabilityStatus.DENIED,
    CapabilityErrorCode.LOCAL_POLICY_DENIED.value: CapabilityStatus.DENIED,
    CapabilityErrorCode.SCOPE_DENIED.value: CapabilityStatus.DENIED,
    # 工作区不可用属于"能力暂时不可用"（不是失败重试）。
    CapabilityErrorCode.WORKSPACE_NOT_BOUND.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.WORKSPACE_DEVICE_OFFLINE.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.WORKSPACE_NOT_REGISTERED.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.WORKSPACE_ROOT_MISSING.value: CapabilityStatus.UNAVAILABLE,
    CapabilityErrorCode.WORKSPACE_PATH_NOT_DIRECTORY.value: CapabilityStatus.FAILED,
}


def capability_status_for(
    *,
    ok: bool = False,
    error_code: str = "",
) -> CapabilityStatus:
    """把结果折叠成前端展示状态（``completed`` / ``failed`` / 具体等待态）。"""
    if ok:
        return CapabilityStatus.COMPLETED
    return ERROR_TO_CAPABILITY_STATUS.get(str(error_code or ""), CapabilityStatus.FAILED)


class CapabilityUsage(BaseModel):
    """用量（审计/计费/限流用；不含正文）。"""

    model_config = ConfigDict(extra="forbid")

    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    queue_ms: int = 0
    bytes_in: int = 0
    bytes_out: int = 0


class CapabilityResult(BaseModel):
    """一次能力调用的结果（客户端 Provider 与服务端 Provider 返回同一种）。"""

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus = ExecutionStatus.SUCCESS
    #: 预算内投影；完整结果按 ``artifact_refs`` 解析。
    payload: Any = None
    schema_name: str = ""
    schema_version: int = 1
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error: ErrorEnvelope | None = None
    sensitivity: str = ""
    usage: CapabilityUsage = Field(default_factory=CapabilityUsage)

    # ── 关联（服务端回填，Provider 回传只用于核对）──
    request_id: str = ""
    trace_id: str = ""
    capability: str = ""
    provider_id: str = ""
    contract_version: int = 1
    #: 流式：下一次要继续读取时带上它（空=已结束）。
    stream_cursor: str = ""
    #: 结果是否来自本地（客户端 Provider 置 True）；投影据此避免把本地正文出网。
    served_locally: bool = False

    @property
    def ok(self) -> bool:
        return self.status.is_ok

    @property
    def error_code(self) -> str:
        return str(self.error.code) if self.error is not None else ""

    @property
    def retryable(self) -> bool:
        if self.error is not None and self.error.retryable:
            return True
        return self.error_code in RETRYABLE_CAPABILITY_ERRORS

    @property
    def needs_install(self) -> bool:
        return self.error_code in INSTALL_REQUIRED_CAPABILITY_ERRORS

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_execution_payload(self) -> dict[str, Any]:
        """投影成既有 ``ToolOutput``/投影层能吃的形状（迁移期兼容入口）。"""
        return {
            "capability": self.capability,
            "provider_id": self.provider_id,
            "contract_version": self.contract_version,
            "status": str(self.status),
            "sensitivity": self.sensitivity,
            "served_locally": self.served_locally,
            "stream_cursor": self.stream_cursor,
        }


def capability_ok(
    payload: Any = None,
    *,
    capability: str = "",
    provider_id: str = "",
    contract_version: int = 1,
    schema_name: str = "",
    schema_version: int = 1,
    artifact_refs: object = None,
    sensitivity: str = "",
    stream_cursor: str = "",
    served_locally: bool = False,
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    request_id: str = "",
    trace_id: str = "",
) -> CapabilityResult:
    """构造成功结果（``partial``/``empty`` 由 ``status`` 指定）。"""
    return CapabilityResult(
        status=status,
        payload=payload,
        schema_name=str(schema_name),
        schema_version=int(schema_version),
        artifact_refs=artifact_refs_from(artifact_refs),
        sensitivity=str(sensitivity),
        stream_cursor=str(stream_cursor),
        served_locally=bool(served_locally),
        capability=str(capability),
        provider_id=str(provider_id),
        contract_version=int(contract_version),
        request_id=str(request_id),
        trace_id=str(trace_id),
    )


def capability_failure(
    code: CapabilityErrorCode | str,
    message: str = "",
    *,
    capability: str = "",
    provider_id: str = "",
    contract_version: int = 1,
    retryable: bool | None = None,
    suggested_action: str = "",
    details: dict[str, Any] | None = None,
    status: ExecutionStatus = ExecutionStatus.FAILED,
    request_id: str = "",
    trace_id: str = "",
) -> CapabilityResult:
    """构造失败结果；``retryable`` 缺省按错误码白名单判定。"""
    key = str(code)
    if retryable is None:
        retryable = key in RETRYABLE_CAPABILITY_ERRORS
    return CapabilityResult(
        status=status,
        capability=str(capability),
        provider_id=str(provider_id),
        contract_version=int(contract_version),
        request_id=str(request_id),
        trace_id=str(trace_id),
        error=ErrorEnvelope(
            code=key,
            message=str(message or key),
            retryable=bool(retryable),
            suggested_action=str(suggested_action),
            details=dict(details or {}),
        ),
    )


def binding_from_result_dispatch(
    *,
    user_id: str,
    conversation_id: str = "",
    workspace_id: str = "",
    device_id: str = "",
    session_id: str = "",
) -> SessionBinding:
    """便捷构造（避免各处重复拼字段）。"""
    return SessionBinding(
        user_id=str(user_id or ""),
        conversation_id=str(conversation_id or ""),
        workspace_id=str(workspace_id or ""),
        device_id=str(device_id or ""),
        session_id=str(session_id or ""),
    )


__all__ = [
    "CapabilityErrorCode",
    "CapabilityResult",
    "CapabilityUsage",
    "ERROR_TO_CAPABILITY_STATUS",
    "INSTALL_REQUIRED_CAPABILITY_ERRORS",
    "RETRYABLE_CAPABILITY_ERRORS",
    "binding_from_result_dispatch",
    "capability_failure",
    "capability_ok",
    "capability_status_for",
]
