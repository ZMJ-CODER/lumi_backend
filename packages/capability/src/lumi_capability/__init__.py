"""``lumi_capability``：能力域的**纯决策内核**（backend-neutral）。

结构重构 P3 的第一个产物。它只做"给定事实，算出结论"这一件事——
不连 Redis、不读 settings、不 import 应用、不依赖编排内核。边界由三处保证：

* ``pyproject.toml`` 只声明 ``lumi-contracts`` 依赖；
* ``tools/check_architecture.py`` 规则 1/5/7（packages 不 import app、包分层、
  包内不得出现运行时设施）；
* ``packages/capability/tests/`` 里的纯函数单测（不 import app 也能跑）。

当前内容（第一批：协议与纯函数；第二批：审计结构、指纹与门禁；
第三批：可见性状态机与工具窗口计划）::

    vocabulary    统一能力名 / 别名 / 资源类型 + 归一
    tiers         副作用与声明 → 审批档位（auto / routine / critical）
    deployment    描述符是否允许在当前部署位置执行
    selection     候选租约选择（能力 → 绑定 → 资源收窄 → 有效期 → 健康 → 心跳）
    fingerprint   能力调用的参数指纹（审批绑定与审计定位）
    audit         审计记录结构 + 本地拒止 → 结构化结果 + 过程条目
    gating        步骤级执行前门禁（目录与抽象词表由调用方注入）
    state         可见性状态机（unregistered / registered / visible / available / unavailable）
    planning      工具窗口计划（意图 + 资源类型 → 窗口；词表与查询由调用方注入）

**刻意留在 app 的**（方案 §P3 的"第一版不抽"清单）：Redis 租约实现、Broker、
MCP 派发、Provider 健康检查、FastAPI 视图、settings 读取、数据库持久化、前端投影，
以及一切"读应用目录/工具注册表"的代码（那是运行时适配，不是纯决策）。
审计的**进程内缓冲与开关**（``CapabilityAuditLog`` / ``audit_enabled``）也留在 app。
"""

from lumi_capability.audit import (
    LOCAL_DENY_REASONS,
    CapabilityAuditRecord,
    audit_record,
    is_local_denial,
    normalize_local_deny_reason,
    process_entry_for_result,
    to_capability_result,
)
from lumi_capability.deployment import descriptor_allows_deployment
from lumi_capability.fingerprint import capability_fingerprint
from lumi_capability.gating import (
    CAPABILITY_ABSTRACT_UNMAPPED,
    CAPABILITY_DEPENDENCY_MISSING,
    CAPABILITY_LOCATION_UNSATISFIABLE,
    NodeCapabilityGate,
    NodeCapabilityIssue,
    declared_capabilities,
    evaluate_node_capabilities,
    node_capability_failure,
    resolve_declared,
)
from lumi_capability.planning import (
    WindowPlan,
    WindowPlanning,
    canonical_tool_for,
    plan_names,
    plan_window,
    read_guards,
)
from lumi_capability.selection import select_lease
from lumi_capability.state import (
    STATE_AVAILABLE,
    STATE_REGISTERED,
    STATE_UNAVAILABLE,
    STATE_UNREGISTERED,
    STATE_VISIBLE,
    VISIBILITY_STATES,
    VisibilityVocabulary,
    resolve_visibility,
    visibility_rank,
    visibility_ranked,
)
from lumi_capability.tiers import (
    SIDE_EFFECT_TIER,
    TIER_AUTO,
    TIER_CRITICAL,
    TIER_ORDER,
    TIER_ROUTINE,
    descriptor_declared_tier,
    floor_for_local_confirmation,
    manifest_requires_local_confirmation,
    manifest_tier_of,
    merge_declared,
    normalize_tier,
    side_effect_tier,
    stricter,
)
from lumi_capability.vocabulary import (
    RESOURCE_ARTIFACT,
    RESOURCE_KNOWLEDGE,
    RESOURCE_MEMORY,
    RESOURCE_OFFICE_DOCUMENT,
    RESOURCE_TYPES,
    RESOURCE_WORKSPACE,
    UNIFIED_ARTIFACT_CREATE,
    UNIFIED_CAPABILITIES,
    UNIFIED_CAPABILITY_ALIASES,
    UNIFIED_CODE_EXECUTE,
    UNIFIED_RESOURCE_DELETE,
    UNIFIED_RESOURCE_EDIT,
    UNIFIED_RESOURCE_MOVE,
    UNIFIED_RESOURCE_READ,
    UNIFIED_RESOURCE_WRITE,
    is_unified_capability,
    normalize_unified_capability,
)

__all__ = [
    "CAPABILITY_ABSTRACT_UNMAPPED",
    "CAPABILITY_DEPENDENCY_MISSING",
    "CAPABILITY_LOCATION_UNSATISFIABLE",
    "LOCAL_DENY_REASONS",
    "NodeCapabilityGate",
    "NodeCapabilityIssue",
    "RESOURCE_ARTIFACT",
    "RESOURCE_KNOWLEDGE",
    "RESOURCE_MEMORY",
    "RESOURCE_OFFICE_DOCUMENT",
    "RESOURCE_TYPES",
    "RESOURCE_WORKSPACE",
    "SIDE_EFFECT_TIER",
    "STATE_AVAILABLE",
    "STATE_REGISTERED",
    "STATE_UNAVAILABLE",
    "STATE_UNREGISTERED",
    "STATE_VISIBLE",
    "TIER_AUTO",
    "TIER_CRITICAL",
    "TIER_ORDER",
    "TIER_ROUTINE",
    "UNIFIED_ARTIFACT_CREATE",
    "UNIFIED_CAPABILITIES",
    "UNIFIED_CAPABILITY_ALIASES",
    "UNIFIED_CODE_EXECUTE",
    "UNIFIED_RESOURCE_DELETE",
    "UNIFIED_RESOURCE_EDIT",
    "UNIFIED_RESOURCE_MOVE",
    "UNIFIED_RESOURCE_READ",
    "UNIFIED_RESOURCE_WRITE",
    "VISIBILITY_STATES",
    "VisibilityVocabulary",
    "WindowPlan",
    "WindowPlanning",
    "CapabilityAuditRecord",
    "audit_record",
    "canonical_tool_for",
    "capability_fingerprint",
    "declared_capabilities",
    "descriptor_allows_deployment",
    "descriptor_declared_tier",
    "evaluate_node_capabilities",
    "floor_for_local_confirmation",
    "is_local_denial",
    "is_unified_capability",
    "manifest_requires_local_confirmation",
    "manifest_tier_of",
    "merge_declared",
    "node_capability_failure",
    "normalize_local_deny_reason",
    "normalize_tier",
    "normalize_unified_capability",
    "plan_names",
    "plan_window",
    "process_entry_for_result",
    "read_guards",
    "resolve_declared",
    "resolve_visibility",
    "select_lease",
    "side_effect_tier",
    "stricter",
    "to_capability_result",
    "visibility_rank",
    "visibility_ranked",
]
