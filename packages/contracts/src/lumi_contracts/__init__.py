"""Lumi 跨模块契约包（backend-neutral，不依赖 ``app``）。

设计原则（与方案一致）：

1. **业务 Payload 自由定义**：业务字段放 ``ExecutionResult.payload``，核心只统一
   运行状态/版本/上下文/错误/投影。
2. **运行控制信息统一**：``ExecutionResult[T]`` 承载 status/error/timing/trace/
   artifact_refs/sensitivity 等控制面字段。
3. **输出投影独立**：模型、UI、审计、持久化四类投影由 ``ProjectionRegistry``
   统一生成，业务结果类不自行拼接模型提示词。
4. **旧组件通过 Adapter 兼容**：遗留裸字典只在 Adapter 内部存在，不向下游扩散。
5. **显式版本**：用 ``lumi.<name>@<n>`` 字符串标识契约版本，不用
   ``XxxV2(XxxV1)`` 继承伪装版本演进。

模块地图::

    lumi_contracts.common        状态 / 错误 / 版本 / 服务端上下文
    lumi_contracts.routing       TaskProfile / RouteDecision / ExecutionRequest
    lumi_contracts.execution     ToolRequest / ToolSpec / ExecutionResult / artifacts
    lumi_contracts.events        统一流式事件 / 生命周期 / 审批
    lumi_contracts.persistence   JobRunView 快照
    lumi_contracts.projections   模型 / UI / 审计 / 持久化投影
    lumi_contracts.adapters      遗留结果适配与工具注册表
"""

from __future__ import annotations

# ── common ───────────────────────────────────────────────
from lumi_contracts.common import (
    ContractError,
    ContractErrorCode,
    ContractVersion,
    ErrorEnvelope,
    ExecutionStatus,
    Sensitivity,
    ServerContext,
    contract_version,
)
from lumi_contracts.common.version import (
    EXECUTION_RESULT,
    JOB_RUN_VIEW,
    KNOWN_CONTRACTS,
    ROUTE_DECISION,
    SKILL_RESULT,
    STREAM_EVENT,
    TASK_PROFILE,
    TOOL_REQUEST,
    TOOL_RESPONSE,
    TOOL_SPEC,
)

# ── execution ────────────────────────────────────────────
from lumi_contracts.execution import (
    ArtifactRef,
    Citation,
    ExecutionResult,
    ExecutionTiming,
    IdempotencyPolicy,
    RetryPolicy,
    RiskLevel,
    SideEffect,
    SkillResult,
    SkillStep,
    ToolRequest,
    ToolSpec,
    artifact_refs_from,
    contract_version_for,
    failure,
    ok,
    skill_step_from_tool_output,
)

# ── routing ──────────────────────────────────────────────
from lumi_contracts.routing import (
    Complexity,
    ExecutionRequest,
    ExecutionTarget,
    InfoSource,
    RouteDecision,
    RouteMode,
    TaskProfile,
)

# ── events ───────────────────────────────────────────────
from lumi_contracts.events import (
    KNOWN_EVENT_TYPES,
    TERMINAL_STATES,
    ApprovalDecision,
    ApprovalScope,
    ApprovalState,
    EventSequencer,
    ProcessKind,
    ProcessLogEntry,
    ProcessStatus,
    RunState,
    StreamEvent,
    StreamEventType,
    approval_fingerprint,
    assert_transition,
    can_transition,
    derive_kind,
    merge_process_log,
    sanitize_process_text,
)

# ── persistence ──────────────────────────────────────────
from lumi_contracts.persistence import JobRunView, StepView

# ── projections ──────────────────────────────────────────
from lumi_contracts.projections import (
    AuditProjection,
    ModelProjection,
    Projection,
    ProjectionKind,
    ProjectionRegistry,
    StorageProjection,
    UiProjection,
    default_projection_registry,
)

# ── adapters ─────────────────────────────────────────────
from lumi_contracts.adapters import (
    DEFAULT_TRUSTED_NAMESPACES,
    LegacyEnvelopeAdapter,
    ToolRegistry,
    UnsupportedContractVersion,
    adapt_tool_result,
    default_tool_registry,
    register_tool,
)

__version__ = "0.1.0"

__all__ = [
    # common
    "ContractError",
    "ContractErrorCode",
    "ContractVersion",
    "ErrorEnvelope",
    "ExecutionStatus",
    "Sensitivity",
    "ServerContext",
    "contract_version",
    "EXECUTION_RESULT",
    "JOB_RUN_VIEW",
    "KNOWN_CONTRACTS",
    "ROUTE_DECISION",
    "SKILL_RESULT",
    "STREAM_EVENT",
    "TASK_PROFILE",
    "TOOL_REQUEST",
    "TOOL_RESPONSE",
    "TOOL_SPEC",
    # execution
    "ArtifactRef",
    "Citation",
    "ExecutionResult",
    "ExecutionTiming",
    "IdempotencyPolicy",
    "RetryPolicy",
    "RiskLevel",
    "SideEffect",
    "SkillResult",
    "SkillStep",
    "ToolRequest",
    "ToolSpec",
    "artifact_refs_from",
    "contract_version_for",
    "failure",
    "ok",
    "skill_step_from_tool_output",
    # routing
    "Complexity",
    "ExecutionRequest",
    "ExecutionTarget",
    "InfoSource",
    "RouteDecision",
    "RouteMode",
    "TaskProfile",
    # events
    "ApprovalDecision",
    "ApprovalScope",
    "ApprovalState",
    "EventSequencer",
    "KNOWN_EVENT_TYPES",
    "ProcessKind",
    "ProcessLogEntry",
    "ProcessStatus",
    "RunState",
    "StreamEvent",
    "StreamEventType",
    "TERMINAL_STATES",
    "approval_fingerprint",
    "assert_transition",
    "can_transition",
    "derive_kind",
    "merge_process_log",
    "sanitize_process_text",
    # persistence
    "JobRunView",
    "StepView",
    # projections
    "AuditProjection",
    "ModelProjection",
    "Projection",
    "ProjectionKind",
    "ProjectionRegistry",
    "StorageProjection",
    "UiProjection",
    "default_projection_registry",
    # adapters
    "DEFAULT_TRUSTED_NAMESPACES",
    "LegacyEnvelopeAdapter",
    "ToolRegistry",
    "UnsupportedContractVersion",
    "adapt_tool_result",
    "default_tool_registry",
    "register_tool",
]
