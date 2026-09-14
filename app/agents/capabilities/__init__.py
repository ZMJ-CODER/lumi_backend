"""插件化/能力化后端（阶段 1 起；内部纵切已完成）。

目录地图（P2：能力域内部纵切，**不出 package、不改行为**）::

    contracts/   能力描述协议、授权事实（纯数据）
      context              AgentExecutionContext：服务端授权事实
    catalog/     目录声明（**两代并存**，见方案 §六）
      legacy               旧一代内置能力目录（CapabilityCatalog + 描述符）
      resource             新一代统一资源能力目录（能力/资源类型/Provider 声明）
      tool_registry        工具注册表条目派生 + 档位/审批 + 影子对拍
    registry/    Provider 协议与注册表
      registry             CapabilityProvider 协议 + CapabilityRegistry
      builtin              现有实现包装成内置 Provider
      resolver             抽象能力 → 具体能力
    policy/      门禁裁决与策略包（纯决策；P3 抽包的主要候选区）
      gate / policy_guard / policy_packs / approvals / routing /
      resource_window / resource_workflow
    broker/      选择与派发
      broker               能力调用入口（服务端唯一转发点）
      dispatch             旧一代派发（静态 CAPABILITY_TOOL_MAP）
      resource_dispatch    新一代派发（统一能力 + 资源类型 + Provider Adapter）
    audit/       审计写入路径（**只搬目录，语义不变**；语义统一单独立项）
      audit
    views/       只读投影（不参与决策）
      views / snapshots / resource_surface

本模块是**包级稳定入口**：``__all__`` 与 P2 之前逐字相同（旧一代公开面），
子包各自刻意不做 re-export。
"""

from __future__ import annotations

from app.agents.capabilities.registry.builtin import (
    PROVIDER_CLIENT_CODE,
    PROVIDER_CLIENT_GIT,
    PROVIDER_CLIENT_WORKSPACE,
    PROVIDER_SERVER_ARTIFACT,
    SERVER_INLINE_CAPABILITIES,
    builtin_providers,
    capability_requires_client,
    register_builtin_providers,
)
from app.agents.capabilities.catalog.legacy import (
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
from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.broker.dispatch import (
    CAPABILITY_TOOL_MAP,
    NEVER_FALLBACK_CAPABILITIES,
    CapabilityDispatchAdapter,
    DispatchOutcome,
    adapt_to_mcp_tool,
    capability_for_mcp_tool,
    mcp_tool_for_capability,
)
from app.agents.capabilities.policy.routing import (
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
from app.agents.capabilities.policy.policy_guard import (
    ApprovalVerdict,
    PluginPolicyGuard,
    PolicyVerdict,
    capability_fingerprint,
    issue_approval_token,
    policy_guard,
    validate_approval,
)
from app.agents.capabilities.policy.policy_packs import (
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
from app.agents.capabilities.registry.registry import (
    CapabilityProvider,
    CapabilityRegistry,
    ProviderRegistration,
    capability_registry,
    descriptor_allows_deployment,
)
from app.agents.capabilities.registry.resolver import (
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
