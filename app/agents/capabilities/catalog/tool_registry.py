"""统一工具注册表（方案 P1）：一个查询入口，七个派生点都从这里取。

## 要解决的问题

现在同一个"工具"的事实散在**五张静态表**里，新增工具必须同时改多处，漏一处就会出现
"MCP 能发现 → 目录能看到 → 预检不认识 → Broker 不知道对应能力 → 执行时变成未知工具"：

| 位置 | 表 | 回答的问题 |
| --- | --- | --- |
| `capabilities/registry/builtin.py` | `TOOL_CAPABILITY_MAP` | 工具 → 能力 |
| `capabilities/catalog/legacy.py` | `IMPLEMENTATION_MAP` | 实现名 → 能力 |
| `capabilities/broker/dispatch.py` | `CAPABILITY_TOOL_MAP` | 能力 → 首选 MCP 工具 |
| `orchestration/capability_preflight.py` | `ACTION_TOOL_WINDOW` | 动作意图 → 工具窗口 |
| 各 Skill / manifest | `allowed_tools` / 声明 | 可见场景、审批、执行环境 |

本模块把这些**派生**出来，收敛成一个 :func:`resolve_tool` / :func:`entries` 查询入口：

```
ToolSpec（影子注册表，插件安装/升级时写入）
    + CapabilityDescriptor（数据本地性/副作用/权限/契约版本）
    + ToolCapability（运行期能力对象：场景/域/审批/环境/确认模式）
        │
        ▼
   ToolRegistryEntry = 工具的全部事实
        │
        ├─ capability        （能力映射）
        ├─ provider_id       （Provider 路由）
        ├─ mcp_target        （MCP 目标）
        ├─ action_intents    （动作窗口）
        ├─ approval_policy   （审批策略）
        ├─ execution_env     （执行环境）
        └─ scenes            （可见场景）
```

## 灰度与真相源（本轮的关键约束）

**默认关闭**（``TOOL_REGISTRY_DERIVED``）：静态表仍是唯一真相源，行为逐字不变——
本模块只提供"如果派生会得到什么"。

打开后：查询优先走派生结果；**静态表作为兜底**（派生拿不到结论时回落到它），
因此不存在"打开开关后某些工具突然查不到"的风险。

无论开关状态，影子对拍（:mod:`app.agents.capabilities.views.tool_shadow`）都能把
"静态 vs 派生"的差异打点出来——这是切换真相源之前必须看的证据（沿用既有的影子期风格）。

## 四分类与拆分结果

方案禁止对这类大文件"整文件搬迁"，要求先按"纯决策 / 运行时适配 / 配置加载 / 业务适配"
四类标注，再逐段拆分、逐段决定去向。本文件的结论：

| 段 | 类别 | 去向与理由 |
| --- | --- | --- |
| 静态词表（意图副作用/档位例外/能力档位/副作用档位/环境词汇） | 配置加载 | ✅ 拆到 :mod:`~app.agents.capabilities.catalog.tool_tables`（纯数据、零依赖，本模块原样再导出） |
| 影子对拍（``shadow_compare`` / ``shadow_parity_totals`` / ``log_shadow_differences``） | 诊断投影 | ✅ 拆到 :mod:`~app.agents.capabilities.views.tool_shadow`（叶子消费者：注册表自己不调用它） |
| ``ToolRegistryEntry`` 数据形状 | 纯决策 | **留在本模块** |
| 灰度开关 ``registry_derived_enabled`` | 配置加载 | **留在本模块** |
| 影子注册表 / Provider / 运行期 ``ToolCapability`` 读取 | 运行时适配 | **留在本模块**（见下） |
| 声明解析与静态回落（``_static_capability`` / ``declared_*``） | 纯决策 | **留在本模块**（P3 抽包候选） |
| 条目装配与缓存（epoch / 目录指纹） | 运行时适配 | **留在本模块** |
| 查询入口（``resolve_tool`` / ``action_window*``） | 纯决策 + 运行时适配 | **留在本模块** |
| 档位与审批派生（``risk_tier_of`` / ``approval_policy_of``） | 纯决策 | **留在本模块**（P3 抽包候选，**禁止**在此引入 IO） |

**为什么"运行时适配"段不继续拆**：它与查询入口、纯决策段**双向调用**
（``entries_by_name`` → ``build_registry_entries``；``risk_tier_of`` → 条目表 →
``_static_capability``），拆开只能靠把"函数内延迟 import"散布到多个模块来解环——
本文件本来就用这种手法绕开环（见各处的函数内 import），再拆一层会把最热路径的
调用关系切碎，收益（少 400 行）不抵风险（读不懂 + 环更难查）。

**它最终的归宿是 P3 的反面**：P3 把**纯决策**段抽进 ``lumi_capability``，
剩下的"运行时适配"本来就该留在 ``app/``——所以现在不拆，等 P3 按类搬运，
边界反而更清楚。

**规则**：往本文件加代码前先想清楚它属于哪一类。若新增的代码需要 IO（网络、文件、
数据库），说明它是运行时适配或业务适配，应当放到别的模块——否则 P3 会被它拖住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from loguru import logger

# 档位的**纯核**在 backend-neutral 的 ``lumi_capability.tiers``（P3 第一批）：
# 副作用/声明 → auto/routine/critical 的算法与契约词表在那里；应用侧的补充表
# （``SIDE_EFFECT_TIER`` 多一个合成名 ``publish``）由本模块作为 ``table=`` 传入。
from lumi_capability.tiers import (
    descriptor_declared_tier as _pkg_descriptor_declared_tier,
)
from lumi_capability.tiers import floor_for_local_confirmation as _pkg_floor_for_local_confirmation
from lumi_capability.tiers import manifest_requires_local_confirmation as _pkg_manifest_requires_local_confirmation
from lumi_capability.tiers import manifest_tier_of as _pkg_manifest_tier_of
from lumi_capability.tiers import normalize_tier as _pkg_normalize_tier
from lumi_capability.tiers import side_effect_tier as _pkg_side_effect_tier
from lumi_capability.tiers import stricter as _pkg_stricter

# ── [配置加载] 静态词表（已拆到 tool_tables，本模块按需使用并**原样再导出**）────
# 再导出是刻意的：既有调用点写的是 ``from ...catalog.tool_registry import SIDE_EFFECT_TIER``，
# 拆分不该改调用面。新代码请直接从 ``catalog.tool_tables`` 取。
from app.agents.capabilities.catalog.tool_tables import (
    CAPABILITY_TIER,
    ENV_CLIENT,
    ENV_SANDBOX,
    ENV_SERVER,
    INTENT_CAPABILITIES,
    INTENT_SIDE_EFFECTS,
    LEGACY_TIER_TOOLS,
    READ_INTENT_PREFIXES,
    SIDE_EFFECT_TIER,
    SUPPLEMENTAL_CAPABILITIES,
    TIER_AUTO,
    TIER_CRITICAL,
    TIER_ROUTINE,
    TOOL_TIER_OVERRIDES,
    _EFFECT_TO_INTENT,
)

#: 全局条目缓存（按 registry epoch 失效）。
_CACHED_ENTRIES: list["ToolRegistryEntry"] | None = None
_CACHED_EPOCH: str | None = None


# ── [纯决策] 数据形状：一个工具的全部事实（不含任何 IO）
@dataclass(frozen=True, slots=True)
class ToolRegistryEntry:
    """一个工具的**全部事实**（由注册协议派生，不再靠人肉同步多张表）。"""

    name: str
    tool_id: str = ""
    capability: str = ""
    #: 服务端内联执行 / 转发客户端 / 未知（``""`` = 本机动作，不参与租约）。
    provider_id: str = ""
    mcp_target: str = ""
    action_type: str = "read"
    action_intents: tuple[str, ...] = ()
    execution_env: str = ENV_SERVER
    approval_policy: str = "none"
    #: 生效审批档位（``auto``/``routine``/``critical``）；声明优先，遗留工具回落静态词表。
    risk_tier: str = ""
    #: ── 统一资源能力层元数据（方案《资源能力层》Phase 1，**加法、不参与判定**）──
    #: 统一能力名（``resource.read`` 这类）：模型与编排只认这一层，底层工具名是 Provider 的事。
    unified_capability: str = ""
    #: 资源类型（``workspace`` / ``office_document`` / …）：Broker 按它选 Provider。
    resource_type: str = ""
    #: 逻辑 Provider 名（``workspace_provider`` …）与候选 Provider（有序，Broker 用）。
    resource_provider: str = ""
    provider_candidates: tuple[str, ...] = ()
    scenes: tuple[str, ...] = ()
    #: 契约版本（来自能力目录；没有能力时为 0）。
    contract_version: int = 0
    data_locality: str = ""
    #: 该工具是否需要确认（审批策略为 ``confirm`` 时为真）。
    requires_confirmation: bool = False
    #: 来源：派生时命中的输入（排障用）。
    sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def requires_lease(self) -> bool:
        """是否需要租约：本地数据能力必须由客户端租约承载（``"": 本机动作``）。"""
        return bool(self.capability) and self.data_locality == "local_only"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_id": self.tool_id,
            "capability": self.capability,
            "provider_id": self.provider_id,
            "mcp_target": self.mcp_target,
            "action_type": self.action_type,
            "action_intents": list(self.action_intents),
            "execution_env": self.execution_env,
            "approval_policy": self.approval_policy,
            "risk_tier": self.risk_tier,
            # 统一资源能力层（Phase 1）：模型/编排读这三个字段，而不是猜底层工具名。
            "unified_capability": self.unified_capability,
            "resource_type": self.resource_type,
            "resource_provider": self.resource_provider,
            "provider_candidates": list(self.provider_candidates),
            "scenes": list(self.scenes),
            "contract_version": self.contract_version,
            "data_locality": self.data_locality,
            "requires_confirmation": self.requires_confirmation,
            "requires_lease": self.requires_lease,
            "sources": list(self.sources),
        }


# ── [配置加载] 灰度开关：派生结果与静态表谁是真相源
def registry_derived_enabled() -> bool:
    """``TOOL_REGISTRY_DERIVED``（默认关闭时静态表仍是唯一真相源）。"""
    try:
        from app.platform.runtime.feature_flags import feature_enabled

        return feature_enabled("TOOL_REGISTRY_DERIVED")
    except Exception:  # noqa: BLE001
        return False


def _capability_meta() -> dict[str, Any]:
    """能力目录的只读元数据（一次取全，避免逐条 ``get`` 的重复开销）。"""
    try:
        from app.agents.capabilities.catalog.legacy import capability_catalog

        return {item.name: item for item in capability_catalog.all()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-registry] 能力目录不可用: {}", str(exc)[:120])
        return {}


# ── [运行时适配] 影子注册表 / Provider 解析 / 运行期 ToolCapability
def _tool_specs() -> dict[str, Any]:
    """影子注册表（插件安装/升级时写入的 ``ToolSpec``）。"""
    try:
        from app.contracts.tools import export_tool_specs

        return export_tool_specs()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-registry] ToolSpec 不可用: {}", str(exc)[:120])
        return {}


def _provider_for_capability(capability: str) -> str:
    """能力 → Provider（服务端内联 / 客户端转发）。

    与 ``builtin.SERVER_INLINE_CAPABILITIES`` 同源：**只有**列表里的能力由服务端执行，
    其余本地数据能力一律表达为"由客户端做"（转发 Provider），因为本地能力在服务端
    执行是安全问题而不是性能问题。
    """
    if not capability:
        return ""
    try:
        from app.agents.capabilities.registry.builtin import (
            PROVIDER_CLIENT_REMOTE,
            PROVIDER_SERVER_ARTIFACT,
            SERVER_INLINE_CAPABILITIES,
        )

        return PROVIDER_SERVER_ARTIFACT if capability in SERVER_INLINE_CAPABILITIES else PROVIDER_CLIENT_REMOTE
    except Exception:  # noqa: BLE001
        return ""


def _action_type_from_capability(cap_name: str, *, effects: set[str]) -> str:
    """工具的动作类型：**从能力副作用派生**（而不是看运行期 ``write_op`` 标志）。

    这一点很容易搞反：静态表里 ``workspace_write`` 只是 ``TOOL_CAPABILITY_MAP`` 的一个
    条目，运行期拿到的 ``ToolCapability`` 未必带 ``write_op=True``（聚合入口/内部工具
    常常不带）。如果按标志判定，写工具会被当成只读，动作窗口与审批策略就全错了。
    """
    if effects & {"write", "delete", "execute", "publish"}:
        return "write"
    if cap_name.startswith("workspace.") and cap_name not in {"workspace.read"}:
        # 兜底：工作区操作族一律按写处理（副作用表万一缺项也不能把它当只读）。
        return "write"
    return "read"


def _static_mcp_target(capability: str) -> str:
    """静态表里的规范入口工具名（**不依赖 dispatch，避免循环**）。

    必须直接读 ``CAPABILITY_TOOL_MAP`` 而不是调用 ``dispatch.mcp_tool_for_capability``：
    后者在开关打开时会反过来查注册表，而注册表条目又要算 ``mcp_target`` —— 那是
    无限递归（实测会被 except 吞掉，表现为"派生结果莫名变成别名"）。
    """
    base = str(capability or "").split("@", 1)[0]
    if not base:
        return ""
    try:
        from app.agents.capabilities.broker.dispatch import CAPABILITY_TOOL_MAP

        return str(CAPABILITY_TOOL_MAP.get(base) or "")
    except Exception:  # noqa: BLE001
        return ""


def _entry_from_tool_capability(capability: Any, *, meta: dict[str, Any]) -> ToolRegistryEntry:
    """从运行期 ``ToolCapability`` 派生（它的字段最全：场景/审批/环境/域）。"""
    name = str(getattr(capability, "name", "") or "")
    # 走纯静态解析 + 声明兜底：这一层是"每个候选工具一次"的热路径，
    # 绝不能触发全局条目重建。
    cap_name, declared_cap = _resolve_capability(name, capability)
    descriptor = meta.get(cap_name)
    side_effects = (
        {str(item) for item in getattr(descriptor, "side_effects", ()) or ()} if descriptor else set()
    )
    intents = tuple(
        sorted(
            intent
            for intent, effects in INTENT_SIDE_EFFECTS.items()
            if effects and (effects & side_effects)
        )
    )
    environment = str(getattr(capability, "environment", "") or ENV_SERVER)
    confirmation = bool(getattr(capability, "requires_confirmation", False))
    mcp_target = _static_mcp_target(cap_name)
    action_type = _action_type_from_capability(cap_name, effects=side_effects)
    scenes = tuple(str(item) for item in (getattr(capability, "scenes", None) or ()) if str(item))
    if not scenes:
        annotations = getattr(capability, "annotations", None) or {}
        scenes = tuple(str(item) for item in (annotations.get("scenes") or ()) if str(item))
    policy = _derive_policy(
        name,
        cap_name,
        capability=capability,
        requires_confirmation=confirmation,
        check_arguments=False,
    )
    return ToolRegistryEntry(
        name=name,
        tool_id=_tool_id(capability),
        capability=cap_name,
        provider_id=_provider_for_capability(cap_name),
        mcp_target=mcp_target or name,
        action_type=action_type,
        action_intents=intents,
        execution_env=environment,
        # 条目上的策略是**派生结论**（声明 + 确认要求 + 档位），不是单一的
        # ``requires_confirmation`` 副本：这样"要不要确认"在各调用点只有一个答案。
        approval_policy=policy,
        risk_tier=_effective_tier_of(name, cap_name, capability),
        **_resource_metadata(name, cap_name, capability),
        scenes=scenes,
        contract_version=int(getattr(descriptor, "contract_version", 0) or 0),
        data_locality=str(getattr(descriptor, "data_locality", "") or ""),
        requires_confirmation=policy == "confirm",
        sources=(
            "ToolCapability",
            "CapabilityDescriptor" if descriptor else "capability:missing",
            "capability:declared" if declared_cap else "capability:static",
        ),
    )


def _tool_id(capability: Any) -> str:
    """复合标识（与 ``skills.mandatory_tools.tool_identity`` 同一实现）。"""
    try:
        from app.agents.skills.mandatory_tools import tool_identity

        return tool_identity(capability)
    except Exception:  # noqa: BLE001
        return str(getattr(capability, "name", "") or "")


# ── [纯决策] 声明解析与静态回落（不查运行期注册表也能给出结论）
def _resource_metadata(name: str, cap_name: str, capability: Any = None) -> dict[str, Any]:
    """统一资源能力层的三个字段（Phase 1：**只加元数据，不参与判定**）。

    刻意不查本注册表（条目构造调它），因此 ``resource_catalog`` 只吃"已经解析好的能力名"。
    绑定失败不是错误：未接入的工具返回空值，管理端会用 ``unbound_tools()`` 把它们列出来
    ——**先看得见，再决定怎么办**。

    ``capability`` 给定时一并读取它的资源类型声明（新资源走这条）。

    两个 Provider 字段的分工（缺口 2a 之后写清楚）：

    * ``resource_provider`` = **声明的**首选 Provider 名（路线设计，可能是"只有声明"）；
    * ``provider_candidates`` = **可用**的候选名（``registered=True`` 且有 ``provider_id``），
      与 Broker 收窄用的是同一条判据。只有声明、没有实现的 Provider **不进候选**——
      否则管理端/排布会把它读成"可用于派发"。
    """
    try:
        from app.agents.capabilities.catalog.resource import (
            binding_for_tool,
            registered_providers_for,
        )

        binding = binding_for_tool(
            name,
            legacy_capability=cap_name,
            resource_type=declared_resource_type_of(name, capability),
        )
        if not binding.known:
            return {}
        candidates = tuple(
            spec.name
            for spec in registered_providers_for(binding.capability, binding.resource_type)
        )
        return {
            "unified_capability": binding.capability,
            "resource_type": binding.resource_type,
            "resource_provider": binding.provider,
            "provider_candidates": candidates,
        }
    except Exception as exc:  # noqa: BLE001 - 元数据缺失不能影响条目构造
        logger.debug("[tool-registry] 资源绑定失败 {}: {}", name, str(exc)[:120])
        return {}


def _static_capability(name: str) -> str:
    """**纯静态**的工具 → 能力（与 ``capability_for_tool`` 关闭开关时的结果逐字一致）。

    条目构造必须用它，不能调 ``capability_for_tool``：后者在开关打开时会反过来查本
    注册表，于是 ``_build_global_entries → 条目构造 → capability_for_tool → resolve_tool
    → _build_global_entries`` 自递归（实测把插件加载拖到 35 秒并刷满 RecursionError）。
    这与 ``_static_mcp_target`` 是同一类修复。
    """
    requested = str(name or "").strip()
    if not requested:
        return ""
    try:
        from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP
    except Exception:  # noqa: BLE001
        return ""
    for candidate in (requested, requested.split("__")[-1], requested.rsplit(".", 1)[-1]):
        if candidate in TOOL_CAPABILITY_MAP:
            return str(TOOL_CAPABILITY_MAP[candidate] or "")
    return ""


def declared_capability_of(tool_name: str, capability: Any = None) -> str:
    """工具/Provider **声明的能力归属**（``workspace.read`` 这类）；``""`` = 未声明。

    与档位声明同一条原则：**静态映射表优先**，声明只在表不认识这个工具时生效。
    理由不是"信不过"，而是路由/租约/审批都建立在表上：允许声明改写既有归属，
    等于让一个插件把 ``workspace_commit`` 说成只读能力。

    ⚠️ 与 ``declared_tier_of`` 一样只查 ``ToolRegistry``／传入的能力对象，**不查**
    本注册表的条目表（否则条目构造自递归）。
    """
    if capability is not None:
        value = _normalize_capability_name(getattr(capability, "capability", ""))
        if value:
            return value
    try:
        from app.agents.skills.registry import ToolRegistry

        tool = ToolRegistry.get(_short_name(tool_name))
        value = _normalize_capability_name(getattr(tool, "capability", "") if tool is not None else "")
        if value:
            return value
    except Exception:  # noqa: BLE001
        return ""
    return ""


def declared_resource_type_of(tool_name: str, capability: Any = None) -> str:
    """工具/Provider **声明的资源类型**（``workspace`` / ``memory`` …）；``""`` = 未声明。

    与能力/档位声明同一口径：只查 ``ToolRegistry`` 与传入的能力对象，**不查**本注册表的
    条目表（否则条目构造会自递归）。
    """
    if capability is not None:
        value = str(getattr(capability, "resource_type", "") or "").strip().casefold()
        if value and value[0].isalpha() and all(ch.isalnum() or ch in "_." for ch in value):
            return value
    try:
        from app.agents.skills.registry import ToolRegistry

        tool = ToolRegistry.get(_short_name(tool_name))
        value = str(getattr(tool, "resource_type", "") or "").strip().casefold()
        if value and value[0].isalpha() and all(ch.isalnum() or ch in "_." for ch in value):
            return value
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _normalize_capability_name(value: Any) -> str:
    """声明式能力名的规范化（去掉 ``@N``，拒绝非点分小写名）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    base = text.split("@", 1)[0]
    if base != base.casefold() or "." not in base:
        return ""
    if not base[0].isalpha() or not all(ch.isalnum() or ch in "._" for ch in base):
        return ""
    return base


def _resolve_capability(name: str, capability: Any = None) -> tuple[str, bool]:
    """工具 → ``(能力, 是否来自声明)``。

    静态表优先；表不认识时才采纳声明。第二个返回值用于把"声明带来的新归属"和
    "表里本来就有的归属"分开披露（影子对比不能把有意差异算成回归）。
    """
    static = _static_capability(name)
    if static:
        return static, False
    if not capability:
        try:
            from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP

            static = str(IMPLEMENTATION_MAP.get(str(name or "").strip()) or "")
        except Exception:  # noqa: BLE001
            static = ""
        if static:
            return static, False
    declared = declared_capability_of(name, capability)
    return (declared, True) if declared else ("", False)



def _entry_from_static_only(name: str, *, meta: dict[str, Any]) -> ToolRegistryEntry:
    """没有运行期 ``ToolCapability`` 时，只用静态表 + 目录派生（MCP/插件工具即是此类）。

    **能力归属以 ``TOOL_CAPABILITY_MAP`` 为准**，``IMPLEMENTATION_MAP`` 只作兜底：
    后者登记的是**服务端实现名**（``workspace_reader``/``git``/``python_exec``…），
    与客户端的工具名同域但不同集合，两者冲突时（``workspace_commit`` 在实现表里是
    ``workspace.write``、在权威路由表里是 ``git.operations``）必须听权威路由表——
    它才是派发/租约/审批实际使用的那张表。

    两张表都不认识时，才采纳工具**自己声明的能力归属**（插件新工具走这条；
    声明不能改写既有归属）。
    """
    cap_name, declared_cap = _resolve_capability(name)
    descriptor = meta.get(cap_name)
    side_effects = (
        {str(item) for item in getattr(descriptor, "side_effects", ()) or ()} if descriptor else set()
    )
    intents = tuple(
        sorted(
            intent
            for intent, effects in INTENT_SIDE_EFFECTS.items()
            if effects and (effects & side_effects)
        )
    )
    mcp_target = _static_mcp_target(cap_name)
    policy = _derive_policy(name, cap_name, check_arguments=False)
    return ToolRegistryEntry(
        name=name,
        capability=cap_name,
        provider_id=_provider_for_capability(cap_name),
        mcp_target=mcp_target or name,
        action_type=_action_type_from_capability(cap_name, effects=side_effects),
        action_intents=intents,
        execution_env=_static_execution_env(name, cap_name),
        approval_policy=policy,
        risk_tier=_effective_tier_of(name, cap_name),
        **_resource_metadata(name, cap_name),
        contract_version=int(getattr(descriptor, "contract_version", 0) or 0),
        data_locality=str(getattr(descriptor, "data_locality", "") or ""),
        requires_confirmation=policy == "confirm",
        # 来源里标出"能力来自声明"：影子对比与排障据此区分"表里本来就有的归属"
        # 与"插件声明带来的新归属"。
        sources=("static", "capability:declared" if declared_cap else "capability:static"),
    )


def _static_execution_env(name: str, cap_name: str) -> str:
    """静态派生的**工作侧**：有能力的看能力，没能力的看它是不是**本机动作**。

    两代对照缺口 2d：``desktop_open_app/url``、``user_clarify`` 这类工具在旧代是
    **显式**的"本机动作"（``TOOL_CAPABILITY_MAP`` 里标 ``None``）。它们没有能力名，
    于是原来的 ``ENV_CLIENT if cap_name else ENV_SERVER`` 会把"在你自己的机器上打开
    记事本"报成**服务端执行**——那是把"不认识"当成了默认值。这里按事实纠正：
    本机动作在客户端执行；真的什么都不认识的工具才回落服务端（保守，不改变选择行为）。
    """
    if cap_name:
        return ENV_CLIENT
    try:
        from app.agents.capabilities.catalog.resource import is_native_action

        if is_native_action(name):
            return ENV_CLIENT
    except Exception:  # noqa: BLE001 - 资源目录不可用时保持原判据
        pass
    return ENV_SERVER


def _effective_tier_of(name: str, cap_base: str, capability: Any = None) -> str:
    """条目上的**生效档位**：派生优先，遗留工具回落静态词表。

    只用于展示/审计（前端管理面板、排障面板），不参与判定——判定仍走
    :func:`risk_tier_of`／``classify_tool_risk``。把生效值放进条目是为了让
    "这个工具到底是什么档"在接口上一眼可见，而不是让运维去读三张表推。
    """
    tier = _derive_tier(name, cap_base, capability=capability, check_arguments=False)
    if tier:
        return tier
    try:
        from app.agents.skills.approval_policy import static_tier_of

        return str(static_tier_of(name, None)[0])
    except Exception:  # noqa: BLE001
        return ""


# ── [运行时适配] 条目装配与缓存（epoch/目录指纹失效）
def build_registry_entries(
    capabilities: Iterable[Any] | None = None,
) -> list[ToolRegistryEntry]:
    """构造注册表条目（静态表 ∪ 目录 ∪ 运行期能力 ∪ 影子注册表）。

    ``capabilities`` 给定时（例如某次请求的候选池）用它派生"这些工具"的条目；
    不给时用静态表与影子注册表的并集，用于审计/预检/派发等全局场景。

    **全局结果带缓存**：派生要读能力目录与影子注册表，逐次重建会让"按能力反查工具"
    变成 O(n²)（实测会把一次预检拖到分钟级）。缓存由 :func:`registry_epoch` 失效——
    插件安装/升级、目录变化都会换 epoch，因此不会读到过期结果。
    """
    if capabilities is not None:
        meta = _capability_meta()
        entries: dict[str, ToolRegistryEntry] = {}
        for capability in capabilities:
            entry = _entry_from_tool_capability(capability, meta=meta)
            if entry.name:
                entries[entry.name] = entry
        return list(entries.values())
    global _CACHED_ENTRIES, _CACHED_EPOCH
    epoch = _current_epoch()
    if _CACHED_ENTRIES is not None and _CACHED_EPOCH == epoch:
        return list(_CACHED_ENTRIES)
    rows = _build_global_entries()
    _CACHED_ENTRIES = list(rows)
    _CACHED_EPOCH = epoch
    return list(rows)


def _current_epoch() -> str:
    """缓存键 = 注册表 epoch + 静态映射表指纹 + **能力目录指纹** + **工具注册表版本**。

    四者缺一不可：

    * 影子注册表/目录摘要（``registry_epoch``）覆盖插件安装与 schema 升级；
    * 静态映射表指纹覆盖"改了表却读到旧派生结果"（实测踩到）；
    * 能力目录指纹覆盖"插件加载前就建了缓存"——那种情况下派生看不到任何能力副作用，
      会把每个工具都判成 ``critical``；
    * 工具注册表版本（``ToolRegistry.version()``）覆盖"ToolSpec 先登记、Skill 后加载"
      ——这种变化**不动** ToolSpec 摘要，只靠上面三项时，"技能还没加载"时算出的条目
      会被永久缓存（实测：``task_memory`` 的能力/资源类型一直读到空绑定，直到进程重启）。
    """
    base = "unknown"
    try:
        from app.agents.skills.mandatory_tools import registry_epoch

        base = registry_epoch()
    except Exception:  # noqa: BLE001
        base = "unknown"
    base = f"{base}#tools:{_tools_version()}"
    try:
        from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP
        from app.agents.capabilities.broker.dispatch import CAPABILITY_TOOL_MAP

        fingerprint = "|".join(
            [
                # 必须带上**值**：只哈希键名时，"改了映射值却不重建缓存"会让派生结果
                # 长时间停在旧值（实测：改完 workspace_commit 的归属后仍读到旧能力）。
                ",".join(f"{k}={v}" for k, v in sorted(TOOL_CAPABILITY_MAP.items())),
                ",".join(f"{k}={v}" for k, v in sorted(CAPABILITY_TOOL_MAP.items())),
            ]
        )
        return f"{base}#{_stable_hash(fingerprint)}#{_catalog_fingerprint()}"
    except Exception:  # noqa: BLE001
        return f"{base}#{_catalog_fingerprint()}"


def _tools_version() -> str:
    """运行期工具注册表的内容版本（``register``/``unregister``/``clear`` 自增）。

    条目的能力/资源类型来自**工具自己声明的** ``capability`` / ``resource_type``，
    而声明由 Skill 注册表承载。这条路径的变化不一定会改动 ToolSpec 影子注册，
    因此必须单独进缓存键（见 :func:`_current_epoch`）。注册表不可用时返回 ``unknown``
    （保守：与"无法确认"同义，宁可多重建一次）。
    """
    try:
        from app.agents.skills.registry import ToolRegistry

        return str(ToolRegistry.version())
    except Exception:  # noqa: BLE001
        return "unknown"


def _catalog_fingerprint() -> str:
    """能力目录指纹（插件加载/目录变化会让它变）。

    为什么缓存键必须含它：能力目录是**插件加载后才填充**的。如果第一次构建缓存发生在
    插件加载之前，之后所有派生都会看不到任何能力副作用 —— 表现为"每个工具都被判成
    critical"（实测就是这个现象）。
    """
    try:
        from app.agents.capabilities.catalog.legacy import capability_catalog

        rows = [
            f"{item.name}@{item.contract_version}:"
            f"{','.join(str(s) for s in (item.side_effects or ()))}"
            for item in capability_catalog.all()
        ]
        return f"{len(rows)}:{_stable_hash('|'.join(sorted(rows)))}"
    except Exception:  # noqa: BLE001
        return "unknown"


def _stable_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _build_global_entries() -> list[ToolRegistryEntry]:
    meta = _capability_meta()
    entries: dict[str, ToolRegistryEntry] = {}
    names: list[str] = []
    try:
        from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

        names.extend(TOOL_CAPABILITY_MAP)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP

        # 以 ``TOOL_CAPABILITY_MAP`` 为准：实现表登记的是**服务端实现名**
        # （workspace_reader/git/python_exec…），与客户端工具名同域但不同集合。
        # 两边冲突时（workspace_commit 在两表里分别是 git.operations / workspace.write）
        # 必须听权威路由表——派发/租约/审批实际用的就是它。
        for name in IMPLEMENTATION_MAP:
            if name not in names:
                names.append(name)
    except Exception:  # noqa: BLE001
        pass
    specs = _tool_specs()
    for qualified in specs:
        # 影子注册表的键是限定名（``lumi.workspace_navigator``）：取末段作为工具名。
        names.append(str(qualified).rsplit(".", 1)[-1])
    for name in dict.fromkeys(str(item) for item in names if str(item)):
        entries[name] = _entry_from_static_only(name, meta=meta)
    return list(entries.values())


def invalidate_cache() -> None:
    """显式失效（测试与"插件刚装完"的主动刷新用）。"""
    global _CACHED_ENTRIES, _CACHED_EPOCH
    _CACHED_ENTRIES = None
    _CACHED_EPOCH = None


# ── [纯决策 + 运行时适配] 查询入口：条目表、工具解析、能力/目标/动作窗口
def entries_by_name(capabilities: Iterable[Any] | None = None) -> dict[str, ToolRegistryEntry]:
    """``{工具名: 条目}``（调用方的主要查询形状）。"""
    return {entry.name: entry for entry in build_registry_entries(capabilities)}


def resolve_tool(tool_name: str, capabilities: Iterable[Any] | None = None) -> ToolRegistryEntry | None:
    """**唯一**的工具查询入口（兼容裸名 / ``mcp__server__tool`` / ``server.tool``）。

    先按派生结果查，查不到再回落到静态表（``capability_for_tool``）——这样"打开开关"
    不会让任何既有工具突然消失。
    """
    requested = str(tool_name or "").strip()
    if not requested:
        return None
    table = entries_by_name(capabilities)
    for candidate in (requested, requested.split("__")[-1], requested.rsplit(".", 1)[-1]):
        entry = table.get(candidate)
        if entry is not None:
            return entry
    # 兜底：静态表认得这个名字，但派生没覆盖（例如新插件的工具还没进目录）。
    try:
        from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

        if requested in TOOL_CAPABILITY_MAP or requested.split("__")[-1] in TOOL_CAPABILITY_MAP:
            name = requested if requested in TOOL_CAPABILITY_MAP else requested.split("__")[-1]
            return _entry_from_static_only(name, meta=_capability_meta())
    except Exception:  # noqa: BLE001
        pass
    return None


def capability_of(tool_name: str) -> str:
    """工具 → 能力（空串 = 本机动作或未知）。

    开关关闭时**直接返回静态表结果**（逐字不变）；打开时用派生结果、静态表兜底。
    """
    if not registry_derived_enabled():
        from app.agents.capabilities.registry.builtin import capability_for_tool

        return str(capability_for_tool(tool_name) or "")
    entry = resolve_tool(tool_name)
    if entry is not None and entry.capability:
        return entry.capability
    from app.agents.capabilities.registry.builtin import capability_for_tool

    return str(capability_for_tool(tool_name) or "")


def mcp_target_for(capability: str, *, fallback: str = "") -> str:
    """能力 → MCP 目标工具（开关关闭时退化为静态 ``CAPABILITY_TOOL_MAP``）。"""
    if not registry_derived_enabled():
        from app.agents.capabilities.broker.dispatch import mcp_tool_for_capability

        return mcp_tool_for_capability(capability, fallback=fallback)
    base = str(capability or "").split("@", 1)[0]
    for entry in build_registry_entries():
        if entry.capability == base and entry.mcp_target:
            return entry.mcp_target
    from app.agents.capabilities.broker.dispatch import mcp_tool_for_capability

    return mcp_tool_for_capability(capability, fallback=fallback)


def action_window(
    intent: str,
    *,
    fallback: tuple[str, ...] = (),
    capabilities: Iterable[Any] | None = None,
) -> tuple[str, ...]:
    """动作意图 → 工具窗口（开关关闭时退化为静态 ``ACTION_TOOL_WINDOW``）。

    两部分组成：

    * **静态对齐部分**（``action_window_static``）：与静态表同构、可逐条对拍的派生；
    * **声明补位部分**（``action_window_declared``）：静态表**根本没有登记**的能力
      （未来新增能力 + 声明了该能力的工具），按副作用交集补进窗口。

    派生规则（与静态表同构，可逐条对拍）：

    * 工具能力的副作用与该意图的副作用**有交集**即进入窗口——
      用交集而不是子集：``git.operations`` 的副作用是 ``{read, execute}``，
      子集判定会让它同时进 READ 与 EXECUTE 两个窗口，而静态表里它哪个都不在
      （它只作为 ``git`` 工具的承载能力存在）。交集同样能保证
      ``workspace.write``（只有 write）不会混进 READ；
    * ``MODIFY`` / ``DELETE`` / ``MOVE`` 额外带上**读取入口**
      （先读到内容才能改，静态表也是这么写的）；
    * 派生结果为空时回落 ``fallback``（例如 ``SEND`` 的 ``send_email`` 不在本地能力
      目录里，靠静态表提供）。
    """
    if not registry_derived_enabled():
        return tuple(fallback)
    key = str(intent or "").strip().upper()
    if key not in INTENT_SIDE_EFFECTS:
        return tuple(fallback)
    names = list(action_window_static(key, fallback=fallback))
    for tool in action_window_declared(key, fallback=tuple(names)):
        if tool not in names:
            names.append(tool)
    return tuple(names)


def action_window_static(intent: str, *, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
    """动作窗口的**静态对齐部分**：只补"静态表登记过的能力"漏掉的规范入口。

    影子对比的对拍基线是这一部分（``action_window_declared`` 是有意差异，见
    :func:`shadow_parity_totals`）。
    """
    key = str(intent or "").strip().upper()
    if key not in INTENT_SIDE_EFFECTS:
        return tuple(fallback)
    # 静态表里的工具是**真的注册过**的（预检要注入存在的东西），因此它始终保留；
    # 派生结果只负责"补上注册表知道、静态表漏了"的规范入口。
    names: list[str] = []
    for tool in fallback:
        if tool not in names:
            names.append(tool)
    for capability in INTENT_CAPABILITIES.get(key, ()):
        if capability not in SUPPLEMENTAL_CAPABILITIES:
            continue
        tool = _canonical_for_capability(capability)
        if not tool or tool in names:
            continue
        # **只注入静态表认得的工具**：规范入口（``sandbox_run``）可能是客户端原子工具名，
        # 服务端并没有同名 Skill。把它写进窗口会让模型看到调不到的名字——预检窗口的
        # 存在意义就是"注入的东西一定存在"。这里用静态判据是为了逐条对拍（见函数说明）。
        if not _is_statically_known_tool(tool):
            continue
        # 读入口放最前（静态表里 MODIFY/DELETE/MOVE 都是这个顺序）；执行类补在后面。
        if key in READ_INTENT_PREFIXES and capability == "workspace.read":
            names.insert(0, tool)
        else:
            names.append(tool)
    return tuple(names)


def _known_intent_capabilities() -> frozenset[str]:
    """**静态表已经覆盖**的能力集合。

    判据不只是 ``INTENT_CAPABILITIES``：``git.operations`` / ``artifact.create`` 也不在
    意图表里，但它们已被静态表登记（有工具级例外/能力级档位），不能被当成"新能力"
    再补一遍——那会让 CREATE 窗口多出 ``create_office_document``，对拍立刻出现差异。
    """
    known = {cap for caps in INTENT_CAPABILITIES.values() for cap in caps}
    known.update(CAPABILITY_TIER)
    return frozenset(known)


def action_window_declared(intent: str, *, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
    """**声明带来的窗口补位**：静态表根本没登记的能力，按副作用交集进入窗口。

    只在能力既不在 ``INTENT_CAPABILITIES``、也不在能力目录（``CAPABILITY_TIER``）时
    生效，因此对既有窗口**零影响**（影子对比里那部分仍然逐条相等）；一旦有插件声明
    了新能力，它就不必再回来改 ``ACTION_TOOL_WINDOW``。补进来的工具同样必须真的注册过。
    """
    key = str(intent or "").strip().upper()
    wanted = INTENT_SIDE_EFFECTS.get(key)
    if not wanted:
        return ()
    known = _known_intent_capabilities()
    names = list(fallback)
    additions: list[str] = []
    for entry in build_registry_entries():
        capability = str(entry.capability or "")
        if not capability or capability in known:
            continue
        effects = _intent_effects_of(capability)
        if not effects or not (effects & wanted):
            continue
        if entry.name in names or entry.name in additions:
            continue
        if not _is_registered_tool(entry.name):
            continue
        additions.append(entry.name)
    return tuple(additions)


def _is_registered_tool(name: str) -> bool:
    """该工具是否**真的可调用**（Skill/插件工具/客户端原子工具）。

    用于"声明补位"：补进窗口的东西必须真的存在。注册表不可用时返回 ``True``
    （保守：宁可多给，也不要漏掉真工具）。
    """
    try:
        from app.agents.orchestration.preflight.capability_preflight_service import tool_registration_facts

        facts = tool_registration_facts()
        if not facts:
            return True  # 注册表不可用时不擅自过滤
        return str(name) in set(facts)
    except Exception:  # noqa: BLE001
        return True


def _is_statically_known_tool(name: str) -> bool:
    """静态表是否认得这个名字（**动作窗口静态对齐部分**的存在性判据）。

    为什么这里不用 :func:`_is_registered_tool`：静态对齐部分必须与
    ``ACTION_TOOL_WINDOW`` 逐条相等，才能作为"切开关不改行为"的对拍基线。用"真的注册过"
    当判据会让派生多注入一些本来不在静态窗口里的规范入口（它们确实存在，但那是**行为
    变化**，应该走申报/灰度，而不是混进对拍里）。
    """
    try:
        from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

        if name in TOOL_CAPABILITY_MAP:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _canonical_for_capability(capability: str) -> str:
    """能力的**规范入口工具名**（``CAPABILITY_TOOL_MAP`` 的登记值）。

    注册表里同一能力下有多个工具别名；"能力 → 首选工具"必须只有一个答案，
    否则动作窗口会一次给模型十几个同义工具。
    """
    if not capability:
        return ""
    try:
        from app.agents.capabilities.broker.dispatch import CAPABILITY_TOOL_MAP

        target = str(CAPABILITY_TOOL_MAP.get(str(capability).split("@", 1)[0]) or "")
        if target:
            return target
    except Exception:  # noqa: BLE001
        pass
    for entry in build_registry_entries():
        if entry.capability == capability and entry.mcp_target:
            return entry.mcp_target
    return ""


def _side_effects_of(capability: str) -> set[str]:
    """能力 → 副作用集合（读能力目录；能力名带 ``@N`` 时按基名查）。"""
    if not capability:
        return set()
    descriptor = _capability_meta().get(str(capability).split("@", 1)[0])
    if descriptor is None:
        return set()
    return {str(item) for item in getattr(descriptor, "side_effects", ()) or ()}


def _intent_effects_of(capability: str) -> set[str]:
    """能力 → **意图词表**的副作用名（经 :data:`_EFFECT_TO_INTENT` 桥接）。"""
    return {_EFFECT_TO_INTENT.get(effect, effect) for effect in _side_effects_of(capability)}


def _stricter(first: str, second: str) -> str:
    """取更严的一档（``""`` = 没有意见）。``critical > routine > auto``。

    纯核已迁到 :func:`lumi_capability.tiers.stricter`；本函数保留名字与签名，
    因为它在文件内被多处调用，且是本模块档位派生的可读入口。
    """
    return _pkg_stricter(first, second)


def _normalize_tier(value: Any) -> str:
    return _pkg_normalize_tier(value)


# ── [纯决策] 档位与审批派生（副作用 → auto/routine/critical → 审批策略）
def side_effect_tier(effects: Any) -> str:
    """副作用集合 → 档位（``""`` = 一个副作用都没声明，不猜）。

    ``effects`` 可以是 ``SideEffectKind`` 枚举、字符串或它们的混合——两种词汇
    （契约枚举与 ``INTENT_SIDE_EFFECTS`` 的合成名）都要认。

    纯核在 :func:`lumi_capability.tiers.side_effect_tier`；本仓库的副作用表比契约词表多一个
    合名 ``publish``（来自 ``INTENT_SIDE_EFFECTS`` 的 SEND/PUBLISH 意图），因此把
    :data:`SIDE_EFFECT_TIER` 作为 ``table`` 传进去——**表留在应用侧，算法在内核里**。
    """
    return _pkg_side_effect_tier(effects, table=SIDE_EFFECT_TIER)


def capability_declared_tier(capability: str) -> str:
    """能力描述符的**声明** → 档位（副作用 + "本机需确认"）。

    Provider/插件在 ``CapabilityDescriptor`` 里声明一次 ``side_effects`` 与
    ``needs_local_confirmation``，新能力就自动有了档位，不必再回来改审批词表。
    **什么都不声明的能力返回 ``""``**（交给调用方的保守默认），而不是猜一个 ``auto``。

    纯核在 :func:`lumi_capability.tiers.descriptor_declared_tier`；本函数只负责
    "从应用能力目录里取出描述符"这一步（运行时适配，留在 app）。
    """
    base = str(capability or "").split("@", 1)[0]
    if not base:
        return ""
    descriptor = _capability_meta().get(base)
    if descriptor is None:
        return ""
    return _pkg_descriptor_declared_tier(descriptor, table=SIDE_EFFECT_TIER)


def _floor_for_local_confirmation(tier: str, needs_local: bool) -> str:
    """"本机必须再确认一次" → 至少 B 档。

    A 档会在真实写入前**静默自动执行**，与"本机要确认"直接矛盾；因此这条只抬不降。
    """
    return _pkg_floor_for_local_confirmation(tier, needs_local)


def manifest_tier_of(manifest: Any) -> str:
    """``PluginManifest`` 的声明 → 档位（副作用 + 权限里的本机确认）。

    这是"插件在 Manifest 里声明一次"的落点：``side_effects`` 决定它会不会改东西，
    ``permissions[].needs_local_confirmation`` 决定要不要人工点头。装到哪一侧、
    跑什么运行时都不影响档位——那是租约与运行方式的事。
    """
    return _pkg_manifest_tier_of(manifest, table=SIDE_EFFECT_TIER)


def manifest_requires_local_confirmation(manifest: Any) -> bool:
    """Manifest 是否要求人工确认（副作用命中审批清单，或任一权限要求本机确认）。"""
    return _pkg_manifest_requires_local_confirmation(manifest)


def declared_tier_of(tool_name: str, capability: Any = None) -> str:
    """工具/Provider 的**自述档位**（不可信声明，只允许用来收紧）。

    两个来源：运行期 ``Tool.risk_tier``（插件类属性即声明）与
    ``ToolCapability.risk_tier``（Provider 随能力一起声明）。非法值视为未声明。

    ⚠️ 本函数只查 ``ToolRegistry``（**不查**本注册表的条目表）：条目构造会反过来问档位，
    若这里再去 ``entries_by_name()`` 就会自递归（实测会把导入拖成分钟级）。
    """
    name = _short_name(tool_name)
    if capability is not None:
        declared = _normalize_tier(getattr(capability, "risk_tier", ""))
        if declared:
            return declared
    try:
        from app.agents.skills.registry import ToolRegistry

        tool = ToolRegistry.get(name)
        declared = _normalize_tier(getattr(tool, "risk_tier", "") if tool is not None else "")
        if declared:
            return declared
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _short_name(tool_name: str) -> str:
    """``mcp__server__workspace_read`` → ``workspace_read``（裸名原样）。"""
    return str(tool_name or "").strip().split("__")[-1].rsplit(".", 1)[-1]


def _capability_baseline(cap_base: str) -> str:
    """能力的**可信基线档位**：工具级例外之外的唯一来源顺序。"""
    return CAPABILITY_TIER.get(str(cap_base or "")) or capability_declared_tier(cap_base) or ""


def _combine_tier(baseline: str, declared: str) -> str:
    """基线 + 自述 → 生效档位（自述只能收紧；无基线时自述即答案）。"""
    if baseline:
        return _stricter(baseline, declared)
    return declared or TIER_CRITICAL


def _derive_tier(
    name: str,
    cap_base: str,
    *,
    arguments: dict[str, Any] | None = None,
    capability: Any = None,
    check_arguments: bool = True,
) -> str | None:
    """档位派生的**无递归内核**（不查条目表，只吃已经解析好的能力名）。

    条目构造必须走这里（``check_arguments=False``）：一旦它反过来调
    :func:`risk_tier_of`，就会 ``resolve_tool → entries_by_name → 条目构造`` 自递归。
    """
    short = _short_name(name).casefold()
    if not short or short in LEGACY_TIER_TOOLS:
        return None
    tier = _combine_tier(
        TOOL_TIER_OVERRIDES.get(short) or _capability_baseline(cap_base),
        declared_tier_of(name, capability),
    )
    if not check_arguments:
        return tier
    args = dict(arguments or {})
    # 参数级升级：与既有审批引擎的两条规则一致（保守命中即升级）。
    has_path = any(args.get(key) not in (None, "") for key in ("path", "file_path", "target_file"))
    path = str(args.get("path") or args.get("file_path") or args.get("target_file") or "").strip()
    recursive = bool(args.get("recursive") or args.get("force") or args.get("-r"))
    if tier != TIER_CRITICAL and _may_delete(short, cap_base):
        root_like = path in {"", ".", "..", "/", "*", "**"} if has_path else short == "workspace_stage_delete"
        if root_like or (recursive and "*" in path):
            return TIER_CRITICAL
    return tier


def risk_tier_of(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    capability: Any = None,
) -> str | None:
    """工具 → 审批档位（``auto`` / ``routine`` / ``critical``）；``None`` = 不派生。

    派生顺序：

    1. **可信基线**：工具级例外 ``TOOL_TIER_OVERRIDES`` → 能力级 ``CAPABILITY_TIER``
       → 能力描述符声明 ``side_effects``/``needs_local_confirmation``
       （``capability_declared_tier``，插件新能力走这条）；
    2. **自述声明**（``Tool.risk_tier`` / ``ToolCapability.risk_tier``）只能**收紧**
       基线，绝不放宽 —— "自述不算授权"（与 ``PluginManifest`` 同一条原则）；
    3. 基线为空（连能力都不认识）时，声明是唯一信息源；仍无声明 → ``critical``
       （**保守**：不认识的东西不能默认自动执行）。

    **返回 ``None``** 表示"这个工具不归注册表管"（遗留/辅助工具，见
    :data:`LEGACY_TIER_TOOLS`）——调用方必须回落到既有静态词表，而不是把 None 当
    "自动执行"。这是安全边界上的显式契约。

    ``arguments`` 只用于参数级升级（与既有 ``classify_tool_risk`` 的两条规则一致：
    删除类工具指向工作区根/通配 → C 档）。``capability`` 可选：Provider 在运行期
    能力对象上声明的档位只有传进来才会被采纳（否则只看工具级声明）。
    """
    name = str(tool_name or "").strip()
    if not name:
        return None
    entry = resolve_tool(name)
    cap_base = str(entry.capability if entry is not None else "").split("@", 1)[0]
    return _derive_tier(name, cap_base, arguments=arguments, capability=capability)


def _may_delete(short: str, cap_base: str) -> bool:
    """该工具是否会删东西（用于参数级升级；判据是能力副作用/动作类型）。"""
    effects = _side_effects_of(cap_base)
    if "delete" in effects:
        return True
    return _action_type_from_capability(cap_base, effects=effects) == "write"


def _needs_local_confirmation(cap_base: str) -> bool:
    """能力描述符是否声明"客户端本机必须再确认一次"（Provider 的 Manifest 声明）。"""
    base = str(cap_base or "").split("@", 1)[0]
    if not base:
        return False
    try:
        descriptor = _capability_meta().get(base)
    except Exception:  # noqa: BLE001
        return False
    return bool(getattr(descriptor, "needs_local_confirmation", False)) if descriptor else False


def _local_confirmation_applies(short: str, cap_base: str) -> bool:
    """能力级的"本机确认"要求是否作用到这个工具。

    **工具级例外优先**：``TOOL_TIER_OVERRIDES`` 是我们自己的、比能力更具体的数据。
    ``workspace_diff`` 属于 ``git.operations``（该能力要求本机确认），但它自己只读、
    静态档位就是 A —— 让能力级信号盖掉工具级事实，会把"只读工具"标成"要确认"，
    于是就又回到"界面一套、执行一套"的老问题。
    """
    if short in TOOL_TIER_OVERRIDES:
        return False
    return _needs_local_confirmation(cap_base)


def _declared_policy(name: str, capability: Any = None) -> str:
    """显式声明的审批策略（``none``/``confirm``；非法值 → ``""``）。"""
    if capability is not None:
        text = str(getattr(capability, "approval_policy", "") or "").strip().casefold()
        if text in {"none", "confirm"}:
            return text
    try:
        from app.agents.skills.registry import ToolRegistry

        tool = ToolRegistry.get(_short_name(name))
        text = str(getattr(tool, "approval_policy", "") or "").strip().casefold()
        return text if text in {"none", "confirm"} else ""
    except Exception:  # noqa: BLE001
        return ""


def _derive_policy(
    name: str,
    cap_base: str,
    *,
    capability: Any = None,
    requires_confirmation: bool = False,
    check_arguments: bool = True,
) -> str:
    """审批策略的**无递归内核**（条目构造走这里，避免自递归）。

    ``confirm`` 的语义是"**可能**要确认"：具体什么时候弹窗由审批网关按
    "帮我确认"开关与授权快照决定（A 档永远不弹，B 档只关着的时候弹一次）。
    """
    short = _short_name(name).casefold()
    local = _local_confirmation_applies(short, cap_base)
    declared = _declared_policy(name, capability)
    if declared == "none" and local:
        # "能力要求本机确认"被声明的 ``none`` 压掉属于**放宽**，与档位同一条原则：
        # 自述只能收紧。
        return "confirm"
    if declared:
        return declared
    if requires_confirmation:
        return "confirm"
    tier = _derive_tier(name, cap_base, capability=capability, check_arguments=check_arguments)
    if tier is None:
        return "none"  # 遗留工具：注册表不派生策略，由静态侧自己决定
    if tier != TIER_AUTO:
        return "confirm"
    return "confirm" if local else "none"


def approval_policy_of(tool_name: str, capability: Any = None) -> str:
    """工具 → 审批策略（``none`` / ``confirm``）。

    顺序：显式声明（``Tool.approval_policy`` / ``ToolCapability.approval_policy``）
    → ``requires_confirmation`` → 能力描述符的 ``needs_local_confirmation``
    → 生效档位为 ``critical``。注册表只回答"要不要确认"，**怎么确认**（一次/每次、
    谁弹窗）仍由审批网关决定。
    """
    entry = resolve_tool(tool_name)
    cap_base = str(entry.capability if entry is not None else "").split("@", 1)[0]
    return _derive_policy(
        tool_name,
        cap_base,
        capability=capability,
        requires_confirmation=bool(entry.requires_confirmation) if entry is not None else False,
    )


def _first_read_entry(capabilities: Iterable[Any] | None) -> str:
    """读取入口名（静态表的 MODIFY/DELETE/MOVE 都用 ``workspace_navigator``）。"""
    try:
        from app.workspace.context import WORKSPACE_NAVIGATOR

        if capabilities is None:
            return str(WORKSPACE_NAVIGATOR)
        names = {str(getattr(item, "name", "") or "") for item in capabilities}
        return str(WORKSPACE_NAVIGATOR) if str(WORKSPACE_NAVIGATOR) in names else ""
    except Exception:  # noqa: BLE001
        return ""


# ── 影子对比（切换真相源之前必须看的证据）────────────────────

#: 参与"能不能切真相源"判定的维度。``tool→risk_tier(declared)`` **不在**其中：
#: 静态词表结构上表达不了插件/Provider 的显式声明，那类差异是有意为之。
__all__ = [
    "CAPABILITY_TIER",
    "ENV_CLIENT",
    "ENV_SANDBOX",
    "ENV_SERVER",
    "INTENT_CAPABILITIES",
    "INTENT_SIDE_EFFECTS",
    "READ_INTENT_PREFIXES",
    "SIDE_EFFECT_TIER",
    "SUPPLEMENTAL_CAPABILITIES",
    "TIER_AUTO",
    "TIER_CRITICAL",
    "TIER_ROUTINE",
    "ToolRegistryEntry",
    "action_window",
    "action_window_declared",
    "action_window_static",
    "approval_policy_of",
    "build_registry_entries",
    "capability_declared_tier",
    "capability_of",
    "declared_capability_of",
    "declared_resource_type_of",
    "declared_tier_of",
    "entries_by_name",
    "invalidate_cache",
    "manifest_requires_local_confirmation",
    "manifest_tier_of",
    "mcp_target_for",
    "registry_derived_enabled",
    "resolve_tool",
    "risk_tier_of",
    "side_effect_tier",
]

