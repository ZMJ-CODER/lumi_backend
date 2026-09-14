"""统一预检与工具窗口（方案《资源能力层》Phase 2）。

## 与 Phase 1 的分工

Phase 1 建了**兼容目录**（谁属于哪种资源上的哪个操作）；Phase 2 让"动作意图 + 资源类型"
成为工具窗口的**真相源**：

```text
action_intent + resource_type
        ↓ required_capability（resource.read / resource.write / …）
        ↓ Registry 候选 Provider（resource_catalog.providers_for）
        ↓ 每个 Provider 的规范工具（CANONICAL_TOOL_BY_CAPABILITY，缺省按注册表定序）
    本轮工具窗口
```

旧表 ``ACTION_TOOL_WINDOW``（意图 → 工具名）**降级为 fallback**：
新窗口永远是"旧窗口 ∪ 资源层派生"，绝不比旧窗口小——否则"新增一个 Provider"就会
变成"某些场景下工具变少"，那是回归而不是迁移。

## 两个必须钉住的约束

1. **核心读取能力不被 Top-K 截断**（Phase 2 验收）：窗口里只要出现某资源的**变更类**
   工具（write/edit/move/delete），该资源的**读取入口**就必须在最终窗口里。
   这条规则同时作用于"生成窗口"（``resource_window``）与"截断"（``read_guards``）。
2. **不认识就不猜**：意图没有统一能力（``SEND``/``PUBLISH`` 属于 deferred 的外部资源）、
   资源类型为空、注册表里没有候选——一律回落到旧窗口，而不是自己编一个工具名。

开关 ``RESOURCE_CAPABILITY_WINDOW``（默认关闭）：关闭时 ``tool_window_for_actions``
逐字走旧路径。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from loguru import logger

from app.agents.capabilities.catalog.resource import (
    RESOURCE_KNOWLEDGE,
    RESOURCE_OFFICE_DOCUMENT,
    RESOURCE_WORKSPACE,
    UNIFIED_ARTIFACT_CREATE,
    UNIFIED_CODE_EXECUTE,
    UNIFIED_RESOURCE_DELETE,
    UNIFIED_RESOURCE_EDIT,
    UNIFIED_RESOURCE_MOVE,
    UNIFIED_RESOURCE_READ,
    UNIFIED_RESOURCE_WRITE,
    binding_for_tool,
    normalize_unified_capability,
    providers_for,
)
from lumi_capability.planning import WindowPlan
from lumi_capability.planning import WindowPlanning as _WindowPlanning
from lumi_capability.planning import canonical_tool_for as _pkg_canonical_tool_for
from lumi_capability.planning import plan_names as _pkg_plan_names
from lumi_capability.planning import plan_window as _pkg_plan_window
from lumi_capability.planning import read_guards as _pkg_read_guards

#: 计划的数据形状与算法都在 ``lumi_capability.planning``（P3 第三批）；
#: 这里保留原公开名 ``ResourceWindowPlan``（**同一个类**，不是复制），
#: 既有类型注解与 ``.as_dict()`` 调用点全部不变。
ResourceWindowPlan = WindowPlan

#: 动作意图 → 统一能力（**Phase 2 的唯一意图映射表**；替代"意图 → 工具名"）。
#:
#: ``SEND`` / ``PUBLISH`` 刻意不在表里：它们的资源在**外部**（邮件/日历/发布平台），
#: 属于 Phase 1 明确推迟的 ``external_service`` 族，需要先有资源类型与 Provider 语义。
INTENT_UNIFIED_CAPABILITY: dict[str, str] = {
    "READ": UNIFIED_RESOURCE_READ,
    "SEARCH": UNIFIED_RESOURCE_READ,
    "CREATE": UNIFIED_RESOURCE_WRITE,
    "MODIFY": UNIFIED_RESOURCE_EDIT,
    "DELETE": UNIFIED_RESOURCE_DELETE,
    "MOVE": UNIFIED_RESOURCE_MOVE,
    "EXECUTE": UNIFIED_CODE_EXECUTE,
}

#: 变更类能力：出现在窗口里就必须带上同资源的读取入口（先读才能改）。
MUTATING_CAPABILITIES: frozenset[str] = frozenset(
    {
        UNIFIED_RESOURCE_WRITE,
        UNIFIED_RESOURCE_EDIT,
        UNIFIED_RESOURCE_MOVE,
        UNIFIED_RESOURCE_DELETE,
    }
)

#: 读取入口排在最前的意图：**所有变更类意图**。
#:
#: 静态表只对 MODIFY/DELETE/MOVE 这么做（CREATE 直接给 workspace_write）。
#: 统一后保持一致口径："先读再改"——包括"创建前先看清目录里有什么"。
READ_FIRST_INTENTS: frozenset[str] = frozenset({"CREATE", "MODIFY", "DELETE", "MOVE"})

#: ``target_scope`` → 资源类型（TaskProfile 的权威字段）。
TARGET_SCOPE_RESOURCES: dict[str, tuple[str, ...]] = {
    "WORKSPACE": (RESOURCE_WORKSPACE,),
    "ATTACHMENT": (RESOURCE_OFFICE_DOCUMENT,),
}

#: ``info_sources`` → 资源类型（画像说"信息从哪来"，据此知道要操作哪种资源）。
INFO_SOURCE_RESOURCES: dict[str, tuple[str, ...]] = {
    "WORKSPACE": (RESOURCE_WORKSPACE,),
    "ATTACHED_FILE": (RESOURCE_OFFICE_DOCUMENT,),
    "INTERNAL_KNOWLEDGE": (RESOURCE_KNOWLEDGE,),
}

#: (统一能力, 资源类型) → **规范工具**（种子表）。
#:
#: 这是 Phase 1 ``CAPABILITY_TOOL_MAP`` 在统一层的等价物，但**只作种子**：
#: 表里没有的组合（新 Provider 的新能力）按注册表定序自动选，不必回头改这里。
#: 判据是"模型该看到哪一个"，不是"哪个实现存在"。
CANONICAL_TOOL_BY_CAPABILITY: dict[tuple[str, str], str] = {
    (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE): "workspace_navigator",
    (UNIFIED_RESOURCE_READ, RESOURCE_OFFICE_DOCUMENT): "office_doc_read",
    (UNIFIED_RESOURCE_READ, RESOURCE_KNOWLEDGE): "query_knowledge",
    (UNIFIED_RESOURCE_WRITE, RESOURCE_WORKSPACE): "workspace_write",
    (UNIFIED_RESOURCE_EDIT, RESOURCE_WORKSPACE): "workspace_edit",
    (UNIFIED_RESOURCE_MOVE, RESOURCE_WORKSPACE): "workspace_move",
    (UNIFIED_RESOURCE_DELETE, RESOURCE_WORKSPACE): "workspace_delete",
    (UNIFIED_RESOURCE_WRITE, RESOURCE_OFFICE_DOCUMENT): "office_doc_edit",
    (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE): "run_in_sandbox",
    (UNIFIED_ARTIFACT_CREATE, "artifact"): "create_office_document",
}


def _field(profile: Any, key: str, default: Any = None) -> Any:
    """画像取值：dict 与对象两种形状都要认（与预检的 ``_profile_field`` 同义）。"""
    if profile is None:
        return default
    if isinstance(profile, Mapping):
        return profile.get(key, default)
    return getattr(profile, key, default)


def _as_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (str, bytes)):
        return [str(value)]
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


def resource_types_for_profile(profile: Any) -> tuple[str, ...]:
    """画像 → 资源类型（有序、去重）。

    ``target_scope`` 优先（它直接说"要动哪类东西"），其次 ``info_sources``
    （"信息从哪来"）。**两者都没有就返回空**——空不是错误，调用方会回落到
    "从旧窗口反推资源类型"（见 :func:`resource_types_from_window`），
    这样既不猜、又不会让没有画像详细字段的老路径失去窗口。
    """
    out: list[str] = []

    def _extend(values: Iterable[str]) -> None:
        for item in values:
            text = str(item or "").strip().upper()
            if text and text not in out:
                out.append(text)

    _extend(_as_list(_field(profile, "target_scope")))
    _extend(_as_list(_field(profile, "info_sources")))
    resources: list[str] = []
    for key in out:
        for candidate in TARGET_SCOPE_RESOURCES.get(key, ()) + INFO_SOURCE_RESOURCES.get(key, ()):
            if candidate not in resources:
                resources.append(candidate)
    return tuple(resources)


def resource_types_from_window(tools: Iterable[str]) -> tuple[str, ...]:
    """从工具窗口反推资源类型（老画像没有 ``target_scope`` 时的回落）。

    反推是**保守**的：只认已经绑定的工具，且只用于"给同一批工具补上读取入口"，
    不会凭空引入别的资源。
    """
    out: list[str] = []
    for name in tools or ():
        binding = binding_for_tool(str(name or ""))
        if binding.known and binding.resource_type not in out:
            out.append(binding.resource_type)
    return tuple(out)


def required_capability(intent: str, resource_type: str = "") -> str:
    """动作意图 → 统一能力；没有对应能力时返回 ``""``（不猜）。"""
    del resource_type  # 目前意图已唯一确定能力；保留参数是为将来"同意图不同资源的例外"
    key = str(intent or "").strip().upper()
    return INTENT_UNIFIED_CAPABILITY.get(key, "")


def canonical_tool_for(
    capability: str,
    resource_type: str,
    *,
    candidates: Iterable[str] = (),
) -> str:
    """(能力, 资源类型) → 规范工具名。

    种子表优先；表里没有时按**注册表候选**定序取第一个（新 Provider 走这条，
    因此"新增 Provider 不改动作映射表"成立）。候选为空则返回 ``""``（不编名字）。

    纯核在 :func:`lumi_capability.planning.canonical_tool_for`；本函数注入本仓库的种子表。
    """
    return _pkg_canonical_tool_for(
        capability, resource_type, canonical_tools=CANONICAL_TOOL_BY_CAPABILITY, candidates=candidates
    )


def _registry_candidates(capability: str, resource_type: str) -> tuple[str, ...]:
    """注册表里属于该 (能力, 资源类型) 的工具（有序、去重）。

    只读注册表条目，不查租约/健康——"能不能用"是 Broker/预检的事，
    窗口只回答"该给模型看什么"。
    """
    unified = normalize_unified_capability(capability)
    if not unified or not resource_type:
        return ()
    try:
        from app.agents.capabilities.catalog.tool_registry import entries_by_name

        entries = entries_by_name()
    except Exception as exc:  # noqa: BLE001 - 注册表不可用时不派生
        logger.debug("[resource-window] 注册表不可用: {}", str(exc)[:120])
        return ()
    providers = {spec.name for spec in providers_for(unified, resource_type)}
    out: list[str] = []
    for entry in entries.values():
        if entry.unified_capability != unified or entry.resource_type != resource_type:
            continue
        if providers and entry.resource_provider and entry.resource_provider not in providers:
            continue
        if entry.name not in out:
            out.append(entry.name)
    return tuple(out)


#: 注入给纯算法的**应用事实**：动作词表、种子表、变更能力集合、
#: 以及三个查询函数（注册表候选 / 候选 Provider / 从工具反推资源类型）。
#: 纯算法在包里，这些"本仓库有什么"在应用侧——这就是纯决策与配置的边界。
_PLANNING = _WindowPlanning(
    intent_capability=INTENT_UNIFIED_CAPABILITY,
    canonical_tools=CANONICAL_TOOL_BY_CAPABILITY,
    mutating_capabilities=MUTATING_CAPABILITIES,
    read_first_intents=READ_FIRST_INTENTS,
    read_capability=UNIFIED_RESOURCE_READ,
    candidates_for=_registry_candidates,
    providers_for=lambda capability, resource_type: tuple(
        spec.name for spec in providers_for(capability, resource_type)
    ),
    resource_types_from_tools=resource_types_from_window,
)


def plan_for_actions(
    action_intents: Iterable[str] | None,
    *,
    resource_types: Iterable[str] = (),
    fallback: Iterable[str] = (),
) -> ResourceWindowPlan:
    """动作意图 + 资源类型 → 资源层窗口计划（纯计算，不改行为）。

    计划算法在 :func:`lumi_capability.planning.plan_window`；本函数把本仓库的
    动作词表、种子表、变更能力集合与三个查询函数**注入**进去（见 :data:`_PLANNING`）。
    """
    return _pkg_plan_window(
        action_intents, planning=_PLANNING, resource_types=resource_types, fallback=fallback
    )


def resource_window(
    action_intents: Iterable[str] | None,
    *,
    resource_types: Iterable[str] = (),
    fallback: Iterable[str] = (),
) -> tuple[str, ...]:
    """动作意图 + 资源类型 → 工具窗口（**旧窗口是子集**，绝不比它小）。"""
    plan = plan_for_actions(
        action_intents, resource_types=resource_types, fallback=fallback
    )
    return plan_names(plan)


def plan_names(plan: ResourceWindowPlan) -> tuple[str, ...]:
    """计划 → 窗口（旧窗口在前，读取入口提到最前，整体去重）。"""
    return _pkg_plan_names(plan, read_first_intents=READ_FIRST_INTENTS)


def read_guards(pool: Iterable[Any]) -> frozenset[str]:
    """截断期的读取保护：某资源有变更类工具时，它的读取入口必须留下。

    Phase 2 验收"新增 workspace_write 不会让 resource.read 消失"就落在这里：
    窗口生成只是"该给模型看什么"，真正的丢失发生在 ``apply_tool_window`` 的截断里，
    因此保护必须作用在**那一层**。返回的名字会被当作强制工具（额外占位，不参与 Top-K）。

    纯核在 :func:`lumi_capability.planning.read_guards`；本函数注入应用的资源绑定
    查询与"该资源的读取入口"查询。
    """
    return _pkg_read_guards(
        pool,
        binding_for=binding_for_tool,
        read_tool_for=lambda resource_type: canonical_tool_for(
            UNIFIED_RESOURCE_READ,
            resource_type,
            candidates=_registry_candidates(UNIFIED_RESOURCE_READ, resource_type),
        ),
        mutating_capabilities=MUTATING_CAPABILITIES,
    )


def window_enabled() -> bool:
    """``RESOURCE_CAPABILITY_WINDOW``（默认关闭：关闭时旧路径逐字不变）。"""
    try:
        from app.platform.runtime.feature_flags import feature_enabled

        return feature_enabled("RESOURCE_CAPABILITY_WINDOW")
    except Exception:  # noqa: BLE001
        return False


def shadow_compare_windows(
    static_windows: Mapping[str, Iterable[str]],
    *,
    resource_types: Iterable[str] = (),
) -> list[list[str]]:
    """静态窗口 vs 资源层窗口的差异行 ``[意图, 静态, 派生]``（**仅披露**）。

    这是 Phase 2 的"切开关前证据"：差异**全部是新增**（资源层从不删工具），
    因此它不能混进 ``switch_safe`` 判定——那是"打开开关对既有工具的影响"的口径。
    仍要披露，是因为运维需要知道"打开后会多出哪些工具"。
    """
    rows: list[list[str]] = []
    for intent, static_tools in sorted(static_windows.items()):
        static_list = [str(item) for item in static_tools]
        plan = plan_for_actions([intent], resource_types=resource_types, fallback=static_list)
        derived = list(plan_names(plan))
        if sorted(derived) != sorted(static_list):
            rows.append([str(intent), ",".join(static_list), ",".join(derived)])
    return rows


__all__ = [
    "CANONICAL_TOOL_BY_CAPABILITY",
    "INFO_SOURCE_RESOURCES",
    "INTENT_UNIFIED_CAPABILITY",
    "MUTATING_CAPABILITIES",
    "READ_FIRST_INTENTS",
    "ResourceWindowPlan",
    "TARGET_SCOPE_RESOURCES",
    "canonical_tool_for",
    "plan_for_actions",
    "plan_names",
    "read_guards",
    "required_capability",
    "resource_types_for_profile",
    "resource_types_from_window",
    "resource_window",
    "shadow_compare_windows",
    "window_enabled",
]
