"""工具窗口的**计划算法**（纯决策）。

从 ``app/agents/capabilities/policy/resource_window.py`` 抽出（结构重构 P3 第三批）。
窗口回答的是"该给模型看什么"，不是"哪个实现存在"——这句话是这一段的全部设计意图，
也是它能被抽出来的原因：**算法是纯的，词表与查询由应用注入**。

三个不变量（原实现如此，抽包后逐字保留）：

1. **窗口只增不减**：``plan_names`` 一定把 ``fallback``（旧静态窗口）放在最前面，
   派生结果只做补充，读入口再被提到最前——所以"打开开关"不会让任何工具消失；
2. **不编名字**：规范工具取自应用的种子表；种子不在候选池里就退回候选里的第一个，
   候选为空就返回空串（**绝不给模型一个不存在的工具名**）；
3. **截断保护作用在截断层**：变更类工具在场时，同资源的读取入口必须进
   ``pinned_reads``，且**必须真的在候选池里**才钉——钉一个不存在的名字会变成
   ``dropped_core`` 噪音，而那是"上游把核心工具过滤掉了"的故障信号，不能滥用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence

from lumi_capability.vocabulary import normalize_unified_capability


def canonical_tool_for(
    capability: str,
    resource_type: str,
    *,
    canonical_tools: Mapping[tuple[str, str], str],
    candidates: Iterable[str] = (),
) -> str:
    """(能力, 资源类型) → 规范工具名。

    种子表优先；表里没有时按**候选**定序取第一个（新 Provider 走这条，
    因此"新增 Provider 不改动作映射表"成立）。候选为空则返回 ``""``（不编名字）。
    """
    unified = normalize_unified_capability(capability)
    seed = canonical_tools.get((unified, str(resource_type or "")))
    pool = [str(item) for item in candidates if str(item)]
    if seed and (not pool or seed in pool):
        return seed
    if seed and pool:
        # 种子工具不在候选池里（未注册/被过滤）→ 用候选里的第一个，
        # 而不是注入一个不存在的名字。
        return pool[0]
    return pool[0] if pool else ""


@dataclass(frozen=True, slots=True)
class WindowPlan:
    """一次工具窗口的**资源层解释**（排障/前端展示/对拍用）。"""

    intents: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()
    #: 能力 → 规范工具（``""`` = 该能力没有可用工具）
    tools_by_capability: tuple[tuple[str, str, str], ...] = ()
    #: 每个 (能力, 资源类型) 的候选 Provider（有序）。
    providers_by_capability: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    pinned_reads: tuple[str, ...] = ()
    derived: tuple[str, ...] = ()
    fallback: tuple[str, ...] = ()
    source: str = "static"

    def as_dict(self) -> dict[str, Any]:
        return {
            "intents": list(self.intents),
            "capabilities": list(self.capabilities),
            "resource_types": list(self.resource_types),
            "tools_by_capability": [
                {"intent": intent, "capability": capability, "tool": tool}
                for intent, capability, tool in self.tools_by_capability
            ],
            "providers_by_capability": [
                {"capability": capability, "resource_type": resource_type, "providers": list(providers)}
                for capability, resource_type, providers in self.providers_by_capability
            ],
            "pinned_reads": list(self.pinned_reads),
            "derived": list(self.derived),
            "fallback": list(self.fallback),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class WindowPlanning:
    """窗口计划需要的**全部应用事实**（词表 + 三个查询函数）。

    这个形状是"纯决策 / 应用配置"边界的书面表达：左边是算法，右边是应用。
    """

    #: 动作意图 → 统一能力（应用的动作词表）。
    intent_capability: Mapping[str, str]
    #: (统一能力, 资源类型) → 规范工具名的**种子**。
    canonical_tools: Mapping[tuple[str, str], str]
    #: 变更类能力（写/删/移动…）：它们在场时同资源的读入口要被钉住。
    mutating_capabilities: frozenset[str]
    #: "读取入口提到最前"的意图集合。
    read_first_intents: frozenset[str]
    #: 读取能力的名字（用于钉读入口）。
    read_capability: str
    #: (能力, 资源类型) → 注册表候选工具（有序）。
    candidates_for: Callable[[str, str], Sequence[str]]
    #: (能力, 资源类型) → 候选 Provider 名（有序）。
    providers_for: Callable[[str, str], Sequence[str]]
    #: 工具名集合 → 资源类型（老画像没有 target_scope 时的保守反推）。
    resource_types_from_tools: Callable[[Sequence[str]], Sequence[str]]


def plan_window(
    action_intents: Iterable[str] | None,
    *,
    planning: WindowPlanning,
    resource_types: Iterable[str] = (),
    fallback: Iterable[str] = (),
) -> WindowPlan:
    """动作意图 + 资源类型 → 资源层窗口计划（纯计算，不改行为）。"""
    intents: list[str] = []
    for item in action_intents or ():
        key = str(item or "").strip().upper()
        if key and key not in intents:
            intents.append(key)
    fallback_tools = [str(item) for item in fallback if str(item)]
    resources = [str(item) for item in resource_types if str(item)]
    if not resources:
        resources = list(planning.resource_types_from_tools(fallback_tools))

    capabilities: list[str] = []
    tools_by_capability: list[tuple[str, str, str]] = []
    providers_by_capability: list[tuple[str, str, tuple[str, ...]]] = []
    derived: list[str] = []
    for intent in intents:
        capability = planning.intent_capability.get(intent, "")
        if not capability:
            continue
        if capability not in capabilities:
            capabilities.append(capability)
        for resource_type in resources:
            candidates = planning.candidates_for(capability, resource_type)
            tool = canonical_tool_for(
                capability, resource_type, canonical_tools=planning.canonical_tools, candidates=candidates
            )
            if not tool:
                continue
            tools_by_capability.append((intent, capability, tool))
            if tool not in derived:
                derived.append(tool)
            provider_names = tuple(planning.providers_for(capability, resource_type))
            key = (capability, resource_type, provider_names)
            if key not in providers_by_capability:
                providers_by_capability.append(key)

    pinned_reads: list[str] = []
    if any(item in planning.mutating_capabilities for item in capabilities):
        for resource_type in resources:
            read_tool = canonical_tool_for(
                planning.read_capability,
                resource_type,
                canonical_tools=planning.canonical_tools,
                candidates=planning.candidates_for(planning.read_capability, resource_type),
            )
            if read_tool and read_tool not in pinned_reads:
                pinned_reads.append(read_tool)

    source = "resource" if derived or pinned_reads else "static"
    return WindowPlan(
        intents=tuple(intents),
        capabilities=tuple(capabilities),
        resource_types=tuple(resources),
        tools_by_capability=tuple(tools_by_capability),
        providers_by_capability=tuple(providers_by_capability),
        pinned_reads=tuple(pinned_reads),
        derived=tuple(derived),
        fallback=tuple(fallback_tools),
        source=source,
    )


def plan_names(plan: WindowPlan, *, read_first_intents: Collection[str]) -> tuple[str, ...]:
    """计划 → 窗口（旧窗口在前，读取入口提到最前，整体去重）。

    **顺序是契约**：``fallback`` 打头保证"窗口只增不减"，
    读入口最后被提到最前（与静态表的 MODIFY/DELETE/MOVE 同构）。
    """
    names: list[str] = []
    for tool in [*plan.fallback, *plan.derived, *plan.pinned_reads]:
        if tool and tool not in names:
            names.append(tool)
    if any(intent in read_first_intents for intent in plan.intents):
        for tool in reversed(plan.pinned_reads):
            if tool in names:
                names.remove(tool)
                names.insert(0, tool)
    return tuple(names)


def read_guards(
    pool: Iterable[Any],
    *,
    binding_for: Callable[[str], Any],
    read_tool_for: Callable[[str], str],
    mutating_capabilities: Collection[str],
) -> frozenset[str]:
    """截断期的读取保护：某资源有变更类工具时，它的读取入口必须留下。

    真正的丢失发生在截断层（Top-K），因此保护作用在那一层；返回的名字会被当作
    强制工具（额外占位，不参与 Top-K）。

    ``binding_for(name)`` 需要返回带 ``known`` / ``capability`` / ``resource_type``
    的对象（即应用的资源绑定形状）；``read_tool_for(resource_type)`` 返回该资源的
    读取入口工具名。
    """
    names: list[str] = []
    resources: list[str] = []
    for item in pool or ():
        name = str(getattr(item, "name", "") or item or "")
        if not name:
            continue
        binding = binding_for(name)
        if not binding.known:
            continue
        if binding.capability in mutating_capabilities and binding.resource_type not in resources:
            resources.append(binding.resource_type)
        names.append(name)
    if not resources:
        return frozenset()
    pool_names = set(names)
    guards: list[str] = []
    for resource_type in resources:
        tool = read_tool_for(resource_type)
        # 只在**候选池里真的有**这个读取入口时才钉住：钉一个不存在的名字会变成
        # dropped_core 噪音（那是"上游把核心工具过滤掉了"的故障信号，不能滥用）。
        if tool and tool in pool_names and tool not in guards:
            guards.append(tool)
    return frozenset(guards)


__all__ = [
    "WindowPlan",
    "WindowPlanning",
    "canonical_tool_for",
    "plan_names",
    "plan_window",
    "read_guards",
]
