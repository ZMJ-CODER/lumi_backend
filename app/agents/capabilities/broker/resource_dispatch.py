"""统一能力派发：Provider Adapter 与结构化派发目标（方案《资源能力层》Phase 3）。

## 它替换掉什么

改造前，"这次调用属于哪个能力"是**从工具名字猜出来的**：

```text
workspace_write  →（静态表）→ workspace.write
```

名字里带什么就认定是什么，于是"新增一个 Provider"必须回来改静态映射表，
而写错的映射会把写操作当只读派发。

改造后，派发拿到的是一份**结构化目标**：

```text
capability        = workspace.write        ← Broker/租约/目录的键（兼容名，客户端协议冻结在它上面）
unified_capability= resource.write         ← 统一能力（模型与编排的表达）
resource_type     = workspace              ← Broker 按它缩小 Provider 候选
provider_id       = lumi.local.workspace   ← 由 Provider Adapter 决定，不由名字猜
mcp_target        = workspace_write        ← 底层原子工具（Adapter 的职责）
```

底层 MCP 名称的转换只发生在 **Provider Adapter** 里：调用方说"对 workspace 做 resource.write"，
Adapter 说"那就是 workspace_write"。

## 三条安全约束

1. **不认识就不猜**：没有绑定的工具返回 ``source="unknown"``、能力为空 →
   调用方落回既有路径（旧 MCP 名兼容解析保留一个版本周期，见方案 §四）；
2. **Adapter 只做转换**：它不判租约、不判健康、不判审批——那些仍然是 Broker 与
   审批网关的职责，这里只回答"名字对应关系"；
3. **收窄候选不倒过来卡死**：Broker 按资源类型缩小 Provider 候选时，如果收窄后
   **一个候选都不剩**（声明不完整/插件没声明资源类型），就保留收窄前的候选并记原因，
   绝不让"声明缺失"变成"工具不可用"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.agents.capabilities.catalog.resource import (
    RESOURCE_PROVIDERS,
    ResourceProviderSpec,
    binding_for_tool,
    label_for_resource,
    normalize_unified_capability,
    providers_for,
)
from app.agents.capabilities.views.resource_surface import display_name_for


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    """一个资源 Provider 的**适配器**（能力 ↔ 底层工具名的转换点）。"""

    name: str
    provider_id: str
    execution_env: str = "client"
    capabilities: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()
    registered: bool = True
    note: str = ""
    #: 兼容期仍然接受的旧物理 id（见 :func:`provider_ids_for`）。
    legacy_provider_ids: tuple[str, ...] = ()

    def supports(self, capability: str, resource_type: str) -> bool:
        unified = normalize_unified_capability(capability)
        return unified in self.capabilities and resource_type in self.resource_types

    @property
    def accepted_provider_ids(self) -> tuple[str, ...]:
        """本 Adapter 接受的**全部**物理 id：首选在前，兼容 id 在后。"""
        return tuple(
            dict.fromkeys(
                item for item in (self.provider_id, *self.legacy_provider_ids) if item
            )
        )

    def mcp_tool_for(self, capability: str, resource_type: str, *, fallback: str = "") -> str:
        """(能力, 资源类型) → 底层原子工具名。

        优先用统一工具注册表里那条 ``mcp_target``（它是"能力 → 规范入口"的既有答案），
        查不到再回落 ``fallback``（调用方手上的工具名），最后才返回空。
        """
        unified = normalize_unified_capability(capability)
        if not unified or not resource_type:
            return str(fallback or "")
        try:
            from app.agents.capabilities.catalog.tool_registry import entries_by_name

            for entry in entries_by_name().values():
                if entry.unified_capability != unified or entry.resource_type != resource_type:
                    continue
                if entry.mcp_target:
                    return str(entry.mcp_target)
        except Exception as exc:  # noqa: BLE001 - 注册表不可用不编名字
            logger.debug("[resource-dispatch] 注册表不可用: {}", str(exc)[:120])
        return str(fallback or "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider_id": self.provider_id,
            "legacy_provider_ids": list(self.legacy_provider_ids),
            "accepted_provider_ids": list(self.accepted_provider_ids),
            "execution_env": self.execution_env,
            "capabilities": list(self.capabilities),
            "resource_types": list(self.resource_types),
            "registered": self.registered,
        }


def _adapter_from_spec(spec: ResourceProviderSpec) -> ProviderAdapter:
    return ProviderAdapter(
        name=spec.name,
        provider_id=spec.provider_id,
        execution_env=spec.execution_env,
        capabilities=tuple(spec.capabilities),
        resource_types=tuple(spec.resource_types),
        registered=spec.registered,
        note=spec.note,
        legacy_provider_ids=tuple(spec.legacy_provider_ids),
    )


def adapters_for(capability: str, resource_type: str) -> tuple[ProviderAdapter, ...]:
    """(能力, 资源类型) → **有序候选 Adapter**（声明顺序即优先级）。"""
    return tuple(_adapter_from_spec(spec) for spec in providers_for(capability, resource_type))


def adapter_for(capability: str, resource_type: str) -> ProviderAdapter | None:
    """首选 Adapter：第一个**已注册且有 provider_id** 的候选（只有声明、没有实现时返回 None）。"""
    for adapter in adapters_for(capability, resource_type):
        if adapter.registered and adapter.provider_id:
            return adapter
    return None


def provider_ids_for(capability: str, resource_type: str) -> frozenset[str]:
    """(能力, 资源类型) → 允许承接的 Provider id 集合（Broker 收窄候选用）。

    **只包含已注册且有 provider_id 的候选**；空集合表示"没有可用声明"，
    调用方必须按"不收窄"处理（见模块说明第 3 条）。

    集合里**同时包含兼容期的旧 id**（``legacy_provider_ids``）：迁移期桌面端可能还在用
    旧 id 注册租约，收窄时必须把两边都算作合法候选，否则"改了声明"会立刻表现成
    "办公文档能力不可用"（缺口 2c 的兼容期契约）。
    """
    wanted: set[str] = set()
    for adapter in adapters_for(capability, resource_type):
        if adapter.registered:
            wanted.update(adapter.accepted_provider_ids)
    return frozenset(wanted)


@dataclass(frozen=True, slots=True)
class DispatchTarget:
    """一次工具调用的**结构化派发目标**（Phase 3 的核心数据结构）。"""

    tool: str
    #: 旧能力名（``workspace.write``）：Broker、租约、能力目录的键。
    capability: str = ""
    unified_capability: str = ""
    resource_type: str = ""
    provider_name: str = ""
    provider_id: str = ""
    #: 底层原子工具（Provider Adapter 的转换结果）。
    mcp_target: str = ""
    source: str = "unknown"

    @property
    def known(self) -> bool:
        """统一层是否认识这次调用。

        判据是**统一能力 + 资源类型**，不是"有没有旧能力名"：纯声明式接入的工具
        （如 ``task_memory``：``resource.write`` + ``memory``）在旧能力目录里没有条目，
        但统一层完全认识它。旧能力名（``workspace.write``）只是 Broker/租约的键，
        缺失时调用方走"服务端内联执行"路径即可。
        """
        return bool(self.unified_capability and self.resource_type)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "unified_capability": self.unified_capability,
            "resource_type": self.resource_type,
            "provider": self.provider_name,
            "provider_id": self.provider_id,
            "mcp_target": self.mcp_target,
            "source": self.source,
            "known": self.known,
        }


def resolve_dispatch(
    tool_name: str,
    *,
    capability_hint: str = "",
    resource_type: str = "",
) -> DispatchTarget:
    """工具名 → 结构化派发目标（**唯一**入口；不认识就返回 ``unknown``，绝不猜）。

    ``capability_hint`` 由调用方给出"已经解析好的能力名"（例如注册表条目里那条，
    可能来自插件声明）；给了就不再查静态表，避免把声明丢掉。
    """
    tool = str(tool_name or "").strip()
    if not tool:
        return DispatchTarget(tool="")
    # 资源类型要一起传下去：统一能力（resource.write）跨多种资源，
    # 只给能力不给资源类型时绑定层会（正确地）判为歧义。
    binding = binding_for_tool(
        tool, legacy_capability=capability_hint, resource_type=resource_type
    )
    capability = str(capability_hint or binding.legacy_capability or "").split("@", 1)[0]
    if not binding.known:
        # 统一层不认识：保留旧能力（如果有）供 Broker 走兼容路径，但不编统一能力
        return DispatchTarget(tool=tool, capability=capability, source="static" if capability else "unknown")
    rtype = str(binding.resource_type or resource_type or "")
    adapter = adapter_for(binding.capability, rtype)
    mcp_target = tool
    try:
        from app.agents.capabilities.catalog.tool_registry import resolve_tool

        entry = resolve_tool(tool)
        if entry is not None and entry.mcp_target:
            mcp_target = str(entry.mcp_target)
    except Exception as exc:  # noqa: BLE001 - 注册表不可用就用原工具名
        logger.debug("[resource-dispatch] 注册表查询失败: {}", str(exc)[:120])
    return DispatchTarget(
        tool=tool,
        capability=capability,
        unified_capability=binding.capability,
        resource_type=rtype,
        provider_name=adapter.name if adapter else (binding.provider or ""),
        provider_id=adapter.provider_id if adapter else (binding.provider_id or ""),
        mcp_target=mcp_target,
        source="resource",
    )


def adapter_tool_for(capability: str, resource_type: str, *, fallback: str = "") -> str:
    """(能力, 资源类型) → 底层原子工具名（Provider Adapter 的正向转换）。

    Workflow Skill / 计划编译这类"先声明能力、再落到工具"的调用方用它，
    因此新增 Provider 不需要改调用点。
    """
    adapter = adapter_for(capability, resource_type)
    if adapter is None:
        return str(fallback or "")
    return adapter.mcp_tool_for(capability, resource_type, fallback=fallback)


def dispatch_labels(tool_name: str) -> dict[str, str]:
    """工具名 → 过程条目/事件用的**结构化标签**（方案《资源能力层》§七）。

    输出七个字段：

    ``capability``     统一能力（``resource.write``）
    ``resource_type``  资源类型（``workspace``）
    ``provider_id``    物理 Provider（``lumi.local.workspace``）——**排障用**，
                       迁移期会改名，前端不要按它分支（缺口 2c）
    ``provider_name``  逻辑 Provider（``workspace_provider``）
    ``provider_kind``  稳定类别（= 资源类型；前端按它分组/取文案，不必理解能力名）
    ``provider_label`` 展示文案（后端固定表给的中文，如"办公文档能力"）
    ``display_name``   模型可见名（收敛关闭时等于工具名，打开时是 ``Write`` 这类对外名）

    前六个是**闭集词汇**，各自过形状闸门；``provider_label`` 来自**固定表**（不是用户输入），
    因此走文案闸门（去路径/凭据 + 限长）。认不出工具就返回空字典——"不认识"不等于
    "可以编一个能力"。工具名本身也过形状闸门：带空格/斜杠/引号的输入直接拒绝，
    避免有人把参数拼进工具名里带出来。
    """
    from lumi_contracts.events.process import label_value, sanitize_process_text

    tool = label_value(tool_name)
    if not tool:
        return {}
    target = resolve_dispatch(tool)
    labels = {
        "capability": target.unified_capability,
        "resource_type": target.resource_type,
        "provider_id": target.provider_id,
        "provider_name": target.provider_name,
        "provider_kind": target.resource_type,
        "provider_label": label_for_resource(target.resource_type),
        "display_name": display_name_for(tool),
    }
    cleaned: dict[str, str] = {}
    for key, value in labels.items():
        if key == "provider_label":
            text: str | None = sanitize_process_text(value, limit=32)
        else:
            text = label_value(value)
        # ``display_name`` 等于工具名时没有信息量（事件里已经有 tool_name）→ 不重复发。
        if key == "display_name" and text == tool:
            continue
        if text:
            cleaned[key] = text
    return cleaned


def dispatch_enabled() -> bool:
    """``RESOURCE_CAPABILITY_DISPATCH``（默认关闭：关闭时旧路径逐字不变）。"""
    try:
        from app.platform.runtime.feature_flags import feature_enabled

        return feature_enabled("RESOURCE_CAPABILITY_DISPATCH")
    except Exception:  # noqa: BLE001
        return False


def adapter_snapshot() -> list[dict[str, Any]]:
    """全部 Adapter 的快照（管理端/排障用）。"""
    return [_adapter_from_spec(spec).as_dict() for spec in RESOURCE_PROVIDERS]


__all__ = [
    "DispatchTarget",
    "ProviderAdapter",
    "adapter_for",
    "adapter_snapshot",
    "adapter_tool_for",
    "adapters_for",
    "dispatch_enabled",
    "provider_ids_for",
    "resolve_dispatch",
]
