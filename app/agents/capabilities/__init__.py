"""插件化/能力化后端（阶段 1 起）。

模块地图::

    context    AgentExecutionContext：服务端授权事实（工作区/项目/审批指纹）
    catalog    内置能力目录：现有能力 → workspace.read/write、code.execute 等
    registry   CapabilityProvider 协议 + CapabilityRegistry（阶段 2 的 Broker 用它选 Provider）
    builtin    现有实现包装成内置 Provider（阶段 1，行为不变）
    broker     能力选择/租约/授权/调用转发（阶段 2）
"""

from __future__ import annotations

from app.agents.capabilities.builtin import (
    PROVIDER_CLIENT_CODE,
    PROVIDER_CLIENT_GIT,
    PROVIDER_CLIENT_WORKSPACE,
    PROVIDER_SERVER_ARTIFACT,
    SERVER_INLINE_CAPABILITIES,
    builtin_providers,
    capability_requires_client,
    register_builtin_providers,
)
from app.agents.capabilities.catalog import (
    CAPABILITY_ARTIFACT_CREATE,
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_CODE_SCAN,
    CAPABILITY_GIT_OPERATIONS,
    CAPABILITY_WORKSPACE_DELETE,
    CAPABILITY_WORKSPACE_EDIT,
    CAPABILITY_WORKSPACE_MOVE,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    IMPLEMENTATION_MAP,
    SERVER_EXECUTABLE_CAPABILITIES,
    WORKSPACE_OPERATION_CAPABILITIES,
    CapabilityCatalog,
    capability_catalog,
)
from app.agents.capabilities.context import AgentExecutionContext
from app.agents.capabilities.dispatch import (
    CAPABILITY_TOOL_MAP,
    NEVER_FALLBACK_CAPABILITIES,
    CapabilityDispatchAdapter,
    DispatchOutcome,
    adapt_to_mcp_tool,
    capability_for_mcp_tool,
    mcp_tool_for_capability,
)
from app.agents.capabilities.routing import (
    MODE_ACTIVE,
    MODE_OFF,
    MODE_READ_ONLY,
    MODE_SHADOW,
    RoutingDecision,
    maybe_route_capability,
    normalize_mode,
    routing_mode,
    should_route,
)
from app.agents.capabilities.policy_guard import (
    ApprovalVerdict,
    PluginPolicyGuard,
    PolicyVerdict,
    capability_fingerprint,
    issue_approval_token,
    policy_guard,
    validate_approval,
)
from app.agents.capabilities.policy_packs import (
    BUILTIN_POLICY_IDS,
    DEFAULT_POLICY_ID,
    POLICY_COST_SAVER,
    POLICY_ENTERPRISE_AUDIT,
    POLICY_HIGH_PRECISION,
    POLICY_MANUAL_COMMIT,
    PolicyPack,
    PolicyPackRegistry,
    policy_packs,
    select_policy_id,
)
from app.agents.capabilities.registry import (
    CapabilityProvider,
    CapabilityRegistry,
    ProviderRegistration,
    capability_registry,
    descriptor_allows_deployment,
)
from app.agents.capabilities.resolver import (
    ABSTRACT_CAPABILITY_MAP,
    CapabilityResolution,
    CapabilityResolver,
    RequiredCapabilitiesReport,
    concrete_capabilities,
)

__all__ = [
    "ABSTRACT_CAPABILITY_MAP",
    "ApprovalVerdict",
    "BUILTIN_POLICY_IDS",
    "CAPABILITY_ARTIFACT_CREATE",
    "CAPABILITY_CODE_EXECUTE",
    "CAPABILITY_CODE_SCAN",
    "CAPABILITY_GIT_OPERATIONS",
    "CAPABILITY_TOOL_MAP",
    "CAPABILITY_WORKSPACE_DELETE",
    "CAPABILITY_WORKSPACE_EDIT",
    "CAPABILITY_WORKSPACE_MOVE",
    "CAPABILITY_WORKSPACE_READ",
    "CAPABILITY_WORKSPACE_WRITE",
    "WORKSPACE_OPERATION_CAPABILITIES",
    "DEFAULT_POLICY_ID",
    "IMPLEMENTATION_MAP",
    "MODE_ACTIVE",
    "MODE_OFF",
    "MODE_READ_ONLY",
    "MODE_SHADOW",
    "NEVER_FALLBACK_CAPABILITIES",
    "POLICY_COST_SAVER",
    "POLICY_ENTERPRISE_AUDIT",
    "POLICY_HIGH_PRECISION",
    "POLICY_MANUAL_COMMIT",
    "PROVIDER_CLIENT_CODE",
    "PROVIDER_CLIENT_GIT",
    "PROVIDER_CLIENT_WORKSPACE",
    "PROVIDER_SERVER_ARTIFACT",
    "PolicyPack",
    "PolicyPackRegistry",
    "PolicyVerdict",
    "PluginPolicyGuard",
    "RoutingDecision",
    "SERVER_EXECUTABLE_CAPABILITIES",
    "SERVER_INLINE_CAPABILITIES",
    "AgentExecutionContext",
    "CapabilityCatalog",
    "CapabilityDispatchAdapter",
    "CapabilityProvider",
    "CapabilityRegistry",
    "CapabilityResolution",
    "CapabilityResolver",
    "DispatchOutcome",
    "ProviderRegistration",
    "RequiredCapabilitiesReport",
    "adapt_to_mcp_tool",
    "builtin_providers",
    "capability_catalog",
    "capability_fingerprint",
    "capability_for_mcp_tool",
    "capability_registry",
    "capability_requires_client",
    "concrete_capabilities",
    "descriptor_allows_deployment",
    "issue_approval_token",
    "maybe_route_capability",
    "mcp_tool_for_capability",
    "normalize_mode",
    "policy_guard",
    "policy_packs",
    "register_builtin_providers",
    "routing_mode",
    "select_policy_id",
    "should_route",
    "validate_approval",
]
