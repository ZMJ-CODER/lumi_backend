"""模型工具面收敛（方案《资源能力层》Phase 5）。

## 目标

模型看到的工具应该只有**少数稳定入口**，而 `workspace_write` / `workspace_stage_write` /
`mcp__lumi_client__workspace_write` / `office_doc_edit` 这些**实现层名字**不该出现：

```text
Read / Write / Edit / Move / Delete / Run / Search
        ↓（执行时按能力+资源类型解析到底层工具，Phase 3 的 Provider Adapter）
workspace_navigator / workspace_write / workspace_edit / Rename / Bash / Glob …
```

## 为什么先做"可见面模型 + 影子对拍"再切换

收敛是**减少模型可见工具**——这是整个改造里唯一会"拿走东西"的一步，做错的代价是
"模型调不到工具"，而它在默认关闭时必须**逐字不变**。因此本模块先提供：

* **可计算的可见面**：给定一批工具，算出收敛后的名字（`converged_face`）；
* **可对拍的差异**（`surface_diff`）：`[原名, 能力, 资源类型, 收敛名]` —— 运维能看到
  "打开开关后哪些名字会消失、变成什么"；
* **别名解析**（`resolve_alias`）：模型叫 `Move` 时，落到哪个真实工具上（按 Provider
  Adapter 与客户端广告名依次尝试）。

## 三条安全约束

1. **分类不了就不隐藏**：没有统一能力绑定的工具（本机动作、编排原语、外部服务）
   原样保留在可见面里——"不认识"绝不能等价于"可以拿走"；
2. **别名必须可解析**：`resolve_alias` 找不到任何可用实现时返回 `None`，
   调用方要报结构化失败（稳定错误码），而不是静默换一个工具；
3. **默认关闭**：`RESOURCE_CAPABILITY_SURFACE` 关闭时注入路径逐字不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.agents.capabilities.catalog.resource import (
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
)

# ── 模型可见工具词表（方案 Phase 5 的七个名字）──────────────

MODEL_READ = "Read"
MODEL_WRITE = "Write"
MODEL_EDIT = "Edit"
MODEL_MOVE = "Move"
MODEL_DELETE = "Delete"
MODEL_RUN = "Run"
MODEL_SEARCH = "Search"

MODEL_FACING_TOOLS: tuple[str, ...] = (
    MODEL_READ,
    MODEL_WRITE,
    MODEL_EDIT,
    MODEL_MOVE,
    MODEL_DELETE,
    MODEL_RUN,
    MODEL_SEARCH,
)

#: 统一能力 → 模型可见名（``Search`` 是 ``resource.read`` 在检索意图下的名字，
#: 因此由 :func:`model_tool_for` 结合意图细化）。
MODEL_TOOL_BY_CAPABILITY: dict[str, str] = {
    UNIFIED_RESOURCE_READ: MODEL_READ,
    UNIFIED_RESOURCE_WRITE: MODEL_WRITE,
    UNIFIED_RESOURCE_EDIT: MODEL_EDIT,
    UNIFIED_RESOURCE_MOVE: MODEL_MOVE,
    UNIFIED_RESOURCE_DELETE: MODEL_DELETE,
    UNIFIED_CODE_EXECUTE: MODEL_RUN,
}

#: **刻意不收敛**的能力：``artifact.create`` 只有服务端一条实现、且办公产物链路
#: 直接引用它。重命名不带来任何收敛收益，风险却是把可用的产物链路改坏。
UNCONVERGED_CAPABILITIES: frozenset[str] = frozenset({UNIFIED_ARTIFACT_CREATE})

#: **七个动词表达不了的工具**：保留原名（归一化后的名字）。
#:
#: 七个对外名是"对某种资源做某个操作"的抽象，表达不了同一资源上的**辅助/阶段动作**：
#: 暂存、提交、回滚、看 diff、准备沙箱、读沙箱输出、重置沙箱。把它们也收敛成
#: ``Write``/``Run`` 会造成两个后果：
#:
#: 1. 语义丢失——"提交"和"写入"变成同一个动词，前端与审计再也分不出；
#: 2. 工具不可达——``collapse_for_surface`` 按对外名去重只留第一个，
#:    ``workspace_commit`` 会被 ``workspace_write`` 挤掉，提交路径彻底消失。
#:
#: 因此这条清单是**收敛的边界**，而不是遗漏：它随"七个动词不够用"一起被验收发现
#: （见 ``tests/capabilities/test_resource_workflow_surface.py``）。
UNCONVERGED_TOOLS: frozenset[str] = frozenset(
    {
        "workspace_commit",
        "workspace_rollback",
        "workspace_diff",
        "workspace_stage_write",
        "workspace_stage_delete",
        "sandbox_prepare",
        "sandbox_output_read",
        "sandbox_reset",
        "sandbox_commit",
    }
)

#: 检索意图：同一个读取能力在检索语义下用 ``Search``。
SEARCH_INTENTS: frozenset[str] = frozenset({"SEARCH"})

#: **只对这些资源类型做收敛**。
#:
#: 为什么必须限定：统一能力跨多种资源（``resource.write`` 既是工作区写入，也是办公文档
#: 编辑、记忆写入）。模型只叫 ``Write`` 时，执行期无法判断该落哪个 Provider——
#: ``resolve_alias`` 没有资源上下文，只能按优先级挑，结果就是"编辑办公文档却调了工作区
#: 写入"。收敛的前提是"资源类型能跟着调用一起走"，那需要 Phase 3 的
#: ``resolve_dispatch(resource_type=…)`` 从任务画像取值；在那之前，只收敛语义唯一的
#: 工作区资源，其余资源（office_document / memory / artifact）**保留原名**。
#: 这条规则是 Phase 6 验收时暴露出来的：``task_memory`` 曾被错误地收敛成 ``Write``。
CONVERGED_RESOURCE_TYPES: frozenset[str] = frozenset({RESOURCE_WORKSPACE})

#: **检索入口**的工具名（归一化：去掉 ``mcp__server__`` 前缀、小写）。
#:
#: ``resource.read`` 同时在"读取"与"检索"两种语义下使用：只按能力映射会让
#: ``Glob``/``Grep`` 也变成 ``Read``，于是对外七个名字里 ``Search`` 永远不出现。
#: 这里显式登记**以检索为主**的入口；聚合入口 ``workspace_navigator`` 不在其中
#: （它主要承担 read，检索通过 ``action=search`` 表达）。
SEARCH_TOOLS: frozenset[str] = frozenset(
    {"glob", "grep", "search", "workspace_search", "code_search", "find"}
)

#: 模型名 → (统一能力, 依次尝试的底层工具名)。
#:
#: 顺序即优先级：先客户端原子名（模型实际可调）、再聚合入口（服务端合成）。
#: ``Read/Write/Edit/Delete`` 与客户端原子名同名（无需改名），``Move/Run/Search``
#: 是"对外的稳定名 vs 客户端历史名"的映射（Rename/Bash/Glob）。
MODEL_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    MODEL_READ: (UNIFIED_RESOURCE_READ, ("Read", "workspace_navigator", "workspace_read")),
    MODEL_WRITE: (UNIFIED_RESOURCE_WRITE, ("Write", "workspace_write")),
    MODEL_EDIT: (UNIFIED_RESOURCE_EDIT, ("Edit", "workspace_edit")),
    MODEL_MOVE: (UNIFIED_RESOURCE_MOVE, ("Rename", "workspace_move")),
    MODEL_DELETE: (UNIFIED_RESOURCE_DELETE, ("Delete", "workspace_delete")),
    MODEL_RUN: (UNIFIED_CODE_EXECUTE, ("Bash", "sandbox_run", "run_in_sandbox", "python_exec")),
    MODEL_SEARCH: (UNIFIED_RESOURCE_READ, ("Glob", "Grep", "workspace_search", "workspace_navigator")),
}


def surface_enabled() -> bool:
    """``RESOURCE_CAPABILITY_SURFACE``（默认关闭：关闭时注入路径逐字不变）。"""
    try:
        from app.platform.runtime.feature_flags import feature_enabled

        return feature_enabled("RESOURCE_CAPABILITY_SURFACE")
    except Exception:  # noqa: BLE001
        return False


def _tool_key(tool_name: str) -> str:
    """工具名归一化（与资源目录同一口径：去前缀、小写）。"""
    return str(tool_name or "").strip().split("__")[-1].rsplit(".", 1)[-1].casefold()


def model_tool_for(
    capability: str,
    *,
    intent: str = "",
    tool: str = "",
    resource_type: str = "",
) -> str:
    """统一能力（+ 资源类型/意图/工具名）→ 模型可见名；**不收敛时返回空**。

    ``Search`` 的判定有两条路（任一命中即可）：意图是检索，或工具本身是检索入口
    （``Glob``/``Grep``/``workspace_search``）。两条都要有，否则同一批工具在不同轮次
    会算出不同的对外名字。

    ``resource_type`` 不在 :data:`CONVERGED_RESOURCE_TYPES` 里时**不收敛**（保留原名）：
    模型只叫 ``Write`` 而执行期不知道资源类型时，会把办公文档编辑派到工作区写入上。
    """
    unified = normalize_unified_capability(str(capability or "").strip())
    if not unified or unified in UNCONVERGED_CAPABILITIES:
        return ""
    if resource_type and str(resource_type) not in CONVERGED_RESOURCE_TYPES:
        return ""
    if tool and _tool_key(tool) in UNCONVERGED_TOOLS:
        # 暂存/提交/回滚/diff/沙箱辅助动作没有对应的对外动词，保留原名
        return ""
    key = str(intent or "").strip().upper()
    if unified == UNIFIED_RESOURCE_READ and (
        key in SEARCH_INTENTS or _tool_key(tool) in SEARCH_TOOLS
    ):
        return MODEL_SEARCH
    return MODEL_TOOL_BY_CAPABILITY.get(unified, "")


@dataclass(frozen=True, slots=True)
class SurfaceEntry:
    """一个工具在收敛后的可见面里的去向。"""

    tool: str
    capability: str = ""
    resource_type: str = ""
    model_tool: str = ""
    #: ``hidden`` = 收敛后不再直接暴露；``kept`` = 保留原名（分类不了或不收敛）。
    action: str = "kept"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "resource_type": self.resource_type,
            "model_tool": self.model_tool,
            "action": self.action,
        }


def surface_entries(tools: Iterable[Any], *, intent: str = "") -> list[SurfaceEntry]:
    """逐个工具算出收敛去向（**分类不了就保留**）。"""
    out: list[SurfaceEntry] = []
    for item in tools or ():
        name = str(getattr(item, "name", "") or item or "")
        if not name:
            continue
        binding = binding_for_tool(name)
        if not binding.known:
            out.append(SurfaceEntry(tool=name, action="kept"))
            continue
        model_tool = model_tool_for(
            binding.capability, intent=intent, tool=name, resource_type=binding.resource_type
        )
        if not model_tool:
            out.append(
                SurfaceEntry(
                    tool=name,
                    capability=binding.capability,
                    resource_type=binding.resource_type,
                    action="kept",
                )
            )
            continue
        out.append(
            SurfaceEntry(
                tool=name,
                capability=binding.capability,
                resource_type=binding.resource_type,
                model_tool=model_tool,
                action="hidden" if name != model_tool else "kept",
            )
        )
    return out


def converged_face(tools: Iterable[Any], *, intent: str = "") -> tuple[str, ...]:
    """收敛后的模型可见工具名（有序、去重；未分类的工具保留原名）。"""
    names: list[str] = []
    for entry in surface_entries(tools, intent=intent):
        name = entry.model_tool or entry.tool
        if name not in names:
            names.append(name)
    return tuple(names)


def surface_diff(tools: Iterable[Any]) -> list[list[str]]:
    """影子对拍行 ``[统一能力, 当前工具名, 收敛后的模型名]``（只列会变化的）。

    行形状与其它影子维度一致（三格），因此前端不需要为它写特殊渲染：
    "静态值"是该能力当前暴露的工具名（逗号分隔），"派生值"是收敛后的模型可见名。
    """
    grouped: dict[str, list[str]] = {}
    converged: dict[str, str] = {}
    for entry in surface_entries(tools):
        if entry.action != "hidden":
            continue
        grouped.setdefault(entry.capability, []).append(entry.tool)
        converged[entry.capability] = entry.model_tool
    return [
        [capability, ",".join(names), converged.get(capability, "")]
        for capability, names in sorted(grouped.items())
    ]


def hidden_tools(tools: Iterable[Any]) -> list[str]:
    """收敛后会从模型可见面消失的具体工具名（排障/前端展示用）。"""
    return [entry.tool for entry in surface_entries(tools) if entry.action == "hidden"]


def resolve_alias(model_name: str, available: Iterable[str]) -> tuple[str, str]:
    """模型名 → ``(真实工具名, 统一能力)``；解析不出返回 ``("", "")``。

    ``available`` 是本次调用实际可用的工具名集合（客户端广告 + 服务端注册）。
    **找不到就返回空**：调用方必须报结构化失败（``MODEL_ALIAS_UNAVAILABLE``），
    不能静默换一个工具——那等于绕过用户批准的那次调用。
    """
    key = str(model_name or "").strip()
    alias = MODEL_ALIASES.get(key)
    if alias is None:
        return "", ""
    capability, preferred = alias
    pool = {str(item or "") for item in available or ()}
    for candidate in preferred:
        if candidate in pool:
            return candidate, capability
    # Provider Adapter 兜底：能力 + workspace 资源上的规范工具（新 Provider 走这条）
    try:
        from app.agents.capabilities.broker.resource_dispatch import adapter_tool_for

        resolved = adapter_tool_for(capability, "workspace")
        if resolved and resolved in pool:
            return resolved, capability
    except Exception:  # noqa: BLE001 - 解析失败按"不可用"处理
        pass
    return "", capability


def is_model_facing(name: str) -> bool:
    return str(name or "").strip() in MODEL_FACING_TOOLS


def display_name_for(capability: Any, *, enabled: bool | None = None) -> str:
    """运行期能力 → **模型本轮实际看到的名字**（开关关闭/不收敛/未登记时返回原名）。

    ``capability`` 可以是 ``ToolCapability`` 对象或工具名字符串。

    ``enabled`` 缺省读 ``RESOURCE_CAPABILITY_SURFACE``；**关闭时恒等**——"模型看到的名字"
    必须等于工具名，不能让过程事件谎报它叫 ``Write``。需要"如果打开会叫什么"的
    **预览**（管理端卡片、影子对拍）请直接用 :func:`model_tool_for`（它不受开关影响）。
    """
    name = str(getattr(capability, "name", "") or capability or "")
    if not name:
        return ""
    active = surface_enabled() if enabled is None else bool(enabled)
    if not active:
        return name
    binding = binding_for_tool(name)
    if not binding.known:
        return name
    return model_tool_for(
        binding.capability, tool=name, resource_type=binding.resource_type
    ) or name


def collapse_for_surface(
    capabilities: Iterable[Any],
    *,
    enabled: bool | None = None,
) -> list[tuple[Any, str]]:
    """渲染前的工具面收敛：``[(capability, 对外名)]``。

    * 关闭（默认）：``对外名 = capability.name``，**不去重**——逐字等于改造前；
    * 打开：多个别名收敛到同一个对外名时**只保留第一个**（候选池已按相关性排序，
      第一个即最相关）；未登记/不收敛的工具保留原名，且不会与别人合并。

    这是"减少模型可见工具"的唯一入口：调用方把返回值逐个喂给 ``make_skill_tool``。
    """
    active = surface_enabled() if enabled is None else bool(enabled)
    out: list[tuple[Any, str]] = []
    if not active:
        for item in capabilities or ():
            out.append((item, str(getattr(item, "name", "") or "")))
        return out
    seen: set[str] = set()
    for item in capabilities or ():
        display = display_name_for(item, enabled=active)
        if not display or display in seen:
            continue
        seen.add(display)
        out.append((item, display))
    return out


def collapse_with_names(
    capabilities: Iterable[Any],
    *,
    enabled: bool | None = None,
) -> tuple[list[tuple[Any, str]], dict[str, str]]:
    """``collapse_for_surface`` + **对外名 → 实现名** 映射。

    第二项给"执行期需要把模型叫的名字解析回实现名"的调用方用（Workflow 里的
    提交/测试前置判断、以及把工具 schema 交给模型之前的一致性检查）。
    关闭收敛时映射为空、对外名即实现名。
    """
    pairs = collapse_for_surface(capabilities, enabled=enabled)
    alias = {
        display: str(getattr(item, "name", "") or "")
        for item, display in pairs
        if display and display != str(getattr(item, "name", "") or "")
    }
    return pairs, alias


def _prompt_name_map() -> dict[str, str]:
    """实现名 → 对外名（只含真正改名的），供 SOP 提示词做运行期翻译。

    候选来自静态工具表与实现表（含 ``mcp__server__tool`` 变体）：提示词里出现的
    工具名基本都在其中；不在其中的名字保持原样（**不猜**）。
    """
    names: list[str] = []
    try:
        from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

        names.extend(str(item) for item in TOOL_CAPABILITY_MAP)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP

        names.extend(str(item) for item in IMPLEMENTATION_MAP)
    except Exception:  # noqa: BLE001
        pass
    mapping: dict[str, str] = {}
    for name in dict.fromkeys(names):
        display = display_name_for(name)
        if display and display != name:
            mapping[name] = display
            mapping[f"mcp__lumi_client__{name}"] = display
            mapping[f"mcp__lumi_pc__{name}"] = display
    return mapping


def translate_prompt_names(text: str, *, enabled: bool | None = None) -> str:
    """把 SOP 提示词里的**实现层工具名**换成对外名（收敛关闭时逐字返回原文）。

    为什么用运行期翻译而不是改提示词文件：`plugins/workflows/prompts/*.md` 是**业务过程
    说明**，由提示词负责人维护；架构迁移不该顺手改它的内容。翻译是确定性的、可对拍的，
    而且关闭开关时原文逐字不变。

    替换按"长名优先"一次扫描完成，避免 ``workspace_write`` 被 ``write`` 抢先替换。
    """
    active = surface_enabled() if enabled is None else bool(enabled)
    raw = str(text or "")
    if not active or not raw:
        return raw
    mapping = _prompt_name_map()
    if not mapping:
        return raw
    import re

    pattern = re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(item) for item in sorted(mapping, key=len, reverse=True)) + r")(?![A-Za-z0-9_])"
    )
    return pattern.sub(lambda match: mapping.get(match.group(1), match.group(1)), raw)


def surface_snapshot() -> dict[str, Any]:
    """收敛词表快照（管理端/测试用；纯数据）。"""
    return {
        "model_facing_tools": list(MODEL_FACING_TOOLS),
        "model_tool_by_capability": dict(MODEL_TOOL_BY_CAPABILITY),
        "unconverged_capabilities": sorted(UNCONVERGED_CAPABILITIES),
        "search_intents": sorted(SEARCH_INTENTS),
        "aliases": {
            name: {"capability": capability, "preferred_tools": list(tools)}
            for name, (capability, tools) in MODEL_ALIASES.items()
        },
    }


__all__ = [
    "CONVERGED_RESOURCE_TYPES",
    "MODEL_ALIASES",
    "MODEL_DELETE",
    "MODEL_EDIT",
    "MODEL_FACING_TOOLS",
    "MODEL_MOVE",
    "MODEL_READ",
    "MODEL_RUN",
    "MODEL_SEARCH",
    "MODEL_TOOL_BY_CAPABILITY",
    "MODEL_WRITE",
    "SEARCH_INTENTS",
    "SEARCH_TOOLS",
    "UNCONVERGED_CAPABILITIES",
    "UNCONVERGED_TOOLS",
    "SurfaceEntry",
    "collapse_for_surface",
    "collapse_with_names",
    "converged_face",
    "display_name_for",
    "hidden_tools",
    "is_model_facing",
    "model_tool_for",
    "resolve_alias",
    "surface_diff",
    "surface_enabled",
    "surface_entries",
    "surface_snapshot",
    "translate_prompt_names",
]
