"""服务端插件子系统（阶段 4 / 5 / 8 的后端部分）。

模块地图::

    state        安装状态存储（JSON，原子写；离线也能安装与回滚）
    signatures   签名校验（hmac-sha256 / ed25519；未配置密钥 = 未验签，不假装有 PKI）
    dependencies 依赖解析（能力 / 插件 / 策略 / 版本；报告形状与既有 dependencies 对齐）
    registry     安装/启用/停用/升级/回滚/健康检查 + 插件快照
    extensions   Extension Handler 登记（未知 kind 默认拒绝）

边界：本包只做**控制面**（登记、门禁、状态机、审计），不做执行加载——第三方插件
必须在 Worker/容器里跑，不在 API 进程内 importlib（见 registry 的隔离门禁）。

命名区分::

    app/plugins/    控制面（Python）：安装、签名、配额、生命周期、扩展登记
    plugins/        内容面（仓库根，非 Python 包）：工具/工作流的 YAML 与实现资产

本包是插件控制面的唯一位置（旧路径 ``app.services.plugins`` 已不再提供）。
"""

from __future__ import annotations

from app.plugins.boundary import (
    PLUGIN_QUOTA_NOT_ENFORCED,
    PLUGIN_WORKER_UNAVAILABLE,
    ExecutionPlan,
    PluginQuotaBlocked,
    QuotaEvidence,
    blocked_error,
    evidence_for,
    plugin_quota_status,
    plugin_quota_statuses,
    record_execution,
    record_refused,
    record_wired,
    reset_quota_evidence,
    resolve_execution,
)
from app.plugins.dependencies import (
    DependencyIssue,
    DependencyReport,
    DependencyResolver,
    InstalledPluginView,
)
from app.plugins.extensions import (
    ExtensionHandler,
    ExtensionHandlerRegistry,
    extension_handlers,
)
from app.plugins.manager import (
    FLAG,
    PluginManager,
    PluginOperationError,
    PluginStepLease,
    active_plugin_manager,
    plugin_blocked_capabilities,
    set_plugin_manager,
)
from app.plugins.quota import (
    GenericOutputArtifactSink,
    PluginConcurrencyGate,
    PluginQuotaSpec,
    PluginWorker,
    PluginWorkerOutcome,
    apply_quota_to_sandbox_result,
    plugin_concurrency_gate,
    quota_spec_for,
)
from app.plugins.registry import (
    PLUGIN_DEPENDENCIES_UNSATISFIED,
    PLUGIN_DEPLOYMENT_NOT_ALLOWED,
    PLUGIN_ISOLATION_TOO_WEAK,
    PLUGIN_KIND_NEEDS_DEV,
    PLUGIN_KIND_UNKNOWN,
    PLUGIN_NOT_INSTALLED,
    PLUGIN_SIGNATURE_INVALID,
    PLUGIN_VERSION_UNAVAILABLE,
    PluginInstallation,
    PluginRegistry,
    PluginRejected,
)
from app.plugins.signatures import (
    SignatureOutcome,
    SignaturePolicy,
    policy_from_settings,
    verify_signature,
)
from app.plugins.state import PluginStateStore, installation_record

__all__ = [
    "DependencyIssue",
    "DependencyReport",
    "DependencyResolver",
    "ExecutionPlan",
    "ExtensionHandler",
    "ExtensionHandlerRegistry",
    "FLAG",
    "GenericOutputArtifactSink",
    "InstalledPluginView",
    "PLUGIN_DEPENDENCIES_UNSATISFIED",
    "PLUGIN_DEPLOYMENT_NOT_ALLOWED",
    "PLUGIN_ISOLATION_TOO_WEAK",
    "PLUGIN_KIND_NEEDS_DEV",
    "PLUGIN_KIND_UNKNOWN",
    "PLUGIN_NOT_INSTALLED",
    "PLUGIN_QUOTA_NOT_ENFORCED",
    "PLUGIN_SIGNATURE_INVALID",
    "PLUGIN_VERSION_UNAVAILABLE",
    "PLUGIN_WORKER_UNAVAILABLE",
    "PluginConcurrencyGate",
    "PluginInstallation",
    "PluginManager",
    "PluginOperationError",
    "PluginQuotaBlocked",
    "PluginQuotaSpec",
    "PluginRegistry",
    "PluginRejected",
    "PluginStateStore",
    "PluginStepLease",
    "PluginWorker",
    "PluginWorkerOutcome",
    "QuotaEvidence",
    "SignatureOutcome",
    "SignaturePolicy",
    "active_plugin_manager",
    "apply_quota_to_sandbox_result",
    "blocked_error",
    "evidence_for",
    "extension_handlers",
    "installation_record",
    "plugin_blocked_capabilities",
    "plugin_concurrency_gate",
    "plugin_quota_status",
    "plugin_quota_statuses",
    "policy_from_settings",
    "quota_spec_for",
    "record_execution",
    "record_refused",
    "record_wired",
    "reset_quota_evidence",
    "resolve_execution",
    "set_plugin_manager",
    "verify_signature",
]
