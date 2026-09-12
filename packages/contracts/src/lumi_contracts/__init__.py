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
    CANONICAL_EVENT_TYPES,
    EVENT_ENVELOPE_VERSION,
    KNOWN_EVENT_TYPES,
    LEGACY_EVENT_ALIASES,
    TERMINAL_STATES,
    ApprovalDecision,
    ApprovalRequiredPayload,
    ApprovalResolvedPayload,
    ApprovalScope,
    ApprovalState,
    ArtifactCreatedPayload,
    CanonicalEventType,
    ControlPayload,
    ErrorPayload,
    EventEnvelope,
    EventSequencer,
    ProcessKind,
    ProcessLogEntry,
    ProcessPayload,
    ProcessStatus,
    RunState,
    StepCompletedPayload,
    StepStartedPayload,
    StreamEvent,
    StreamEventType,
    TextDeltaPayload,
    ViewUpdatedPayload,
    approval_fingerprint,
    assert_transition,
    build_envelope,
    build_payload,
    bounded_opaque_value,
    can_transition,
    canonical_event_type,
    canonical_events_for,
    dedupe_envelopes,
    derive_kind,
    is_canonical_event_type,
    make_event_id,
    merge_process_log,
    sanitize_process_text,
    strip_unsafe_payload,
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

# ── plugins（插件化/能力化契约：阶段 0 冻结）──────────────
from lumi_contracts.plugins import (
    ACTIVATABLE_PLUGIN_KINDS,
    APPROVAL_REQUIRED_SIDE_EFFECTS,
    DEVELOPER_ONLY_PLUGIN_KINDS,
    ERROR_TO_CAPABILITY_STATUS,
    INSTALL_REQUIRED_CAPABILITY_ERRORS,
    RETRYABLE_CAPABILITY_ERRORS,
    VIEW_DATA_MAX_BYTES,
    VIEW_TYPES,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    CapabilityStatus,
    CapabilityUsage,
    DataLocality,
    Deployment,
    IsolationLevel,
    PluginEntrypoints,
    PluginHealthcheck,
    PluginKind,
    PluginManifest,
    PluginPermission,
    PluginProvides,
    PluginRef,
    PluginRequires,
    PluginResourceLimits,
    PluginSignature,
    PluginSnapshot,
    PolicyRef,
    ProviderHealth,
    ProviderLease,
    ProviderRef,
    SessionBinding,
    SideEffectKind,
    TrustLevel,
    ViewContribution,
    capability_failure,
    capability_ok,
    capability_status_for,
    deployment_allows,
    isolation_for_trust,
    parse_data_locality,
    parse_plugin_kind,
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
    "ApprovalRequiredPayload",
    "ApprovalResolvedPayload",
    "ApprovalScope",
    "ApprovalState",
    "ArtifactCreatedPayload",
    "CANONICAL_EVENT_TYPES",
    "CanonicalEventType",
    "ControlPayload",
    "EVENT_ENVELOPE_VERSION",
    "ErrorPayload",
    "EventEnvelope",
    "EventSequencer",
    "KNOWN_EVENT_TYPES",
    "LEGACY_EVENT_ALIASES",
    "ProcessKind",
    "ProcessLogEntry",
    "ProcessPayload",
    "ProcessStatus",
    "RunState",
    "StepCompletedPayload",
    "StepStartedPayload",
    "StreamEvent",
    "StreamEventType",
    "TERMINAL_STATES",
    "TextDeltaPayload",
    "ViewUpdatedPayload",
    "approval_fingerprint",
    "assert_transition",
    "build_envelope",
    "build_payload",
    "bounded_opaque_value",
    "can_transition",
    "canonical_event_type",
    "canonical_events_for",
    "dedupe_envelopes",
    "derive_kind",
    "is_canonical_event_type",
    "make_event_id",
    "merge_process_log",
    "sanitize_process_text",
    "strip_unsafe_payload",
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
    # plugins（插件化/能力化）
    "ACTIVATABLE_PLUGIN_KINDS",
    "APPROVAL_REQUIRED_SIDE_EFFECTS",
    "DEVELOPER_ONLY_PLUGIN_KINDS",
    "ERROR_TO_CAPABILITY_STATUS",
    "INSTALL_REQUIRED_CAPABILITY_ERRORS",
    "RETRYABLE_CAPABILITY_ERRORS",
    "VIEW_DATA_MAX_BYTES",
    "VIEW_TYPES",
    "CapabilityBinding",
    "CapabilityDescriptor",
    "CapabilityErrorCode",
    "CapabilityInvocation",
    "CapabilityResult",
    "CapabilityStatus",
    "CapabilityUsage",
    "DataLocality",
    "Deployment",
    "IsolationLevel",
    "PluginEntrypoints",
    "PluginHealthcheck",
    "PluginKind",
    "PluginManifest",
    "PluginPermission",
    "PluginProvides",
    "PluginRef",
    "PluginRequires",
    "PluginResourceLimits",
    "PluginSignature",
    "PluginSnapshot",
    "PolicyRef",
    "ProviderHealth",
    "ProviderLease",
    "ProviderRef",
    "SessionBinding",
    "SideEffectKind",
    "TrustLevel",
    "ViewContribution",
    "capability_failure",
    "capability_ok",
    "capability_status_for",
    "deployment_allows",
    "isolation_for_trust",
    "parse_data_locality",
    "parse_plugin_kind",
]
