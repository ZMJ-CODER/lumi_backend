"""必须保留工具 + 工具链路四层快照（方案 §五 P0）。

## 要解决的两个真实故障

**1. 读工具被写工具挤掉。** 候选池里至少 6 处 ``[:8]`` / ``[:3]`` 截断（ChatGraph 最终池、
Office 初选、ReAct 每轮、会话缓存合并、域发现后、工作区阶段注入）。它们都是**普通 Top-K
竞争**，于是"新装一个写工具"就可能把 ``workspace_navigator`` 挤出模型可见窗口——
表现是"模型识别不到读工具"，而工具其实一直是好的。

结论：**核心工具必须脱离 Top-K 竞争**。候选上限只约束**可选**工具，不约束强制工具。

**2. 工具在哪一层消失查不出来。** 链路是
``catalog → 场景/权限过滤 → 排序候选 → 传给模型``，任何一层都可能丢工具，但没有一层
留证据。:class:`ToolWindowSnapshot` 把四层都记下来（只记名字与原因，不含参数/正文），
排障时一眼看出"少了哪个、在哪一层少的、为什么"。

## 强制工具的定义

:data:`CORE_TOOLS` 是**唯一**的强制清单，只放"少了它整个功能就残废"的工具。当前只有
一个：``workspace_navigator``（工作区读取的唯一入口）。**不要**往里加普通工具——每加
一个都会挤占可选工具的空间，而可选工具之间的竞争正是要保留的能力。

调用方还可以**按当前任务**追加临时强制项（:func:`pin_tools`）：例如本轮明确要写文件，
就把写工具钉住，而不是让它在排序里碰运气。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from loguru import logger

#: 必须保留的工具（唯一清单；新增前先读本模块 docstring 的警告）。
CORE_TOOLS: frozenset[str] = frozenset(
    {
        # 工作区读取的唯一入口：被挤出窗口 = 模型"看不见读工具"。
        "workspace_navigator",
    }
)

#: 三态可见性词汇（方案 §五 P1——不要把三种状态混成"工具是否存在"）。
STATE_CATALOG = "catalog"      # Catalog：系统知道这个工具
STATE_ELIGIBLE = "eligible"    # Eligible：当前用户/场景允许使用
STATE_AVAILABLE = "available"  # Available：Provider 健康且租约有效（可调用）
STATE_UNAVAILABLE = "unavailable"  # 允许使用，但当前没有可用 Provider（≠ 不存在）

#: 可用性提示词 → 三态。``availability_hint`` 由能力构造处写入（MCP 探测/租约）。
_HINT_TO_STATE: dict[str, str] = {
    "available": STATE_AVAILABLE,
    "healthy": STATE_AVAILABLE,
    "offline": STATE_UNAVAILABLE,
    "unavailable": STATE_UNAVAILABLE,
    "degraded": STATE_UNAVAILABLE,
    "unhealthy": STATE_UNAVAILABLE,
}


def visibility_state(capability: Any) -> str:
    """从 ToolCapability 归一出**三态可见性**（方案 §五 P1）。

    为什么值得单独一个词表：Provider 暂时离线时，工具在旧实现里会"像从系统消失一样"，
    模型无法区分"没有这个工具"与"工具暂时不可用"，于是会去编一个替代做法。
    """
    annotations = getattr(capability, "annotations", None) or {}
    hint = str(annotations.get("availability_hint") or "").strip().casefold()
    if hint:
        state = _HINT_TO_STATE.get(hint)
        if state:
            return state
        return STATE_ELIGIBLE
    # MCP 工具没写 hint 时：``environment=client`` 且没有租约信息 → 只能算 eligible。
    return STATE_ELIGIBLE


def tool_identity(capability: Any) -> str:
    """工具的**唯一内部标识**：``plugin_id|provider_id|name@version``。

    裸工具名**不是**可靠主键：两个插件提供同名工具时后者会覆盖前者
    （``ToolDiscoverySession.loaded_tools`` 就是按 name 存的）。展示名可以保持简洁，
    但注册、缓存、租约、执行都必须用这个复合键。

    缺字段时用 ``-`` 占位，保持纯函数、不抛错。
    """
    annotations = getattr(capability, "annotations", None) or {}
    plugin_id = str(annotations.get("plugin_id") or annotations.get("plugin") or "-")
    provider_id = str(
        annotations.get("provider_id") or annotations.get("provider") or getattr(capability, "server", "") or "-"
    )
    name = str(getattr(capability, "name", "") or "")
    version = str(getattr(capability, "version", "") or "1.0.0")
    return f"{plugin_id}|{provider_id}|{name}@{version}"


def is_mandatory(capability: Any, *, extra: Iterable[str] = ()) -> bool:
    """是否是强制工具（核心清单 ∪ 调用方本轮显式钉住的）。"""
    name = str(getattr(capability, "name", "") or "")
    if not name:
        return False
    return name in CORE_TOOLS or name in set(extra or ())


def pin_tools(*names: str) -> frozenset[str]:
    """本轮临时强制项（与 :data:`CORE_TOOLS` 求并集后使用）。

    用途：本轮明确要写文件 → 把写工具钉住，不让它在排序里碰运气。**不是**用来把
    整个工具表都钉死——那等于取消候选上限，模型上下文会被工具 schema 塞满。
    """
    return frozenset(str(name) for name in names if str(name).strip())


@dataclass(slots=True)
class TrimResult:
    """一次"上限内选工具"的结果（含强制工具保命信息）。"""

    capabilities: list[Any]
    pinned: list[str] = field(default_factory=list)
    """被保住的强制工具名（按最终顺序）。"""
    dropped_core: list[str] = field(default_factory=list)
    """**强制但已不在候选池里**的名字。

    这不是截断的错，而是**上游过滤**丢掉的核心工具（场景/权限/在线状态），必须显式
    暴露：以前这种情况只会表现为"模型说没有读工具"，现在能直接看出是哪一层丢的。
    """
    trimmed_optional: list[str] = field(default_factory=list)
    """被上限裁掉的可选工具名（原来"悄悄消失"的就是它们）。"""


def trim_with_mandatory(
    capabilities: Sequence[Any],
    *,
    limit: int,
    extra_mandatory: Iterable[str] = (),
) -> TrimResult:
    """在 ``limit`` 内选工具，**强制工具永不参与竞争**（方案 §五 P0-1）。

    ``limit`` 的语义是**可选工具的预算**（不是最终总数）：强制工具额外占位。
    这是刻意的——旧实现的 bug 正是"把强制工具也算进总数"，于是池子一满就把读工具
    挤出去。宁可最终窗口是 ``limit + N(强制)``，也不能让核心工具消失。

    规则（顺序即优先级）：

    1. 强制工具按候选池原顺序**先占位**（原顺序 = 上层的排序结果，因此保留排序语义）；
    2. 可选工具按原顺序填满 ``limit`` 个名额，超出的记入 ``trimmed_optional``；
    3. 候选池里**已经不存在**的强制工具记入 ``dropped_core``（上游过滤导致，需告警）。
       ——"强制工具超过 limit"不存在：limit 只约束可选工具。
    """
    rows = [item for item in capabilities or () if str(getattr(item, "name", "") or "")]
    wanted = set(CORE_TOOLS) | set(extra_mandatory or ())
    cap = max(0, int(limit or 0))

    pinned: list[Any] = [item for item in rows if str(getattr(item, "name", "")) in wanted]
    optional: list[Any] = [item for item in rows if str(getattr(item, "name", "")) not in wanted]

    seen = {str(getattr(item, "name", "")) for item in pinned}
    dropped_core = sorted(name for name in wanted if name not in seen)

    kept_optional = optional[:cap]
    trimmed = optional[cap:]

    result = TrimResult(
        capabilities=[*pinned, *kept_optional],
        pinned=[str(getattr(item, "name", "")) for item in pinned],
        dropped_core=dropped_core,
        trimmed_optional=[str(getattr(item, "name", "")) for item in trimmed],
    )
    if result.dropped_core:
        # 核心工具缺位是**故障信号**：工具链路上游把它过滤掉了（场景/权限/在线）。
        logger.warning(
            "[tool-window] 强制工具不在候选池中（上游过滤）: {} limit={}",
            ",".join(result.dropped_core),
            cap,
        )
    return result


# ── 四层快照（方案 §五 P0-2）─────────────────────────────────

#: 快照分层的固定名称（日志与前端按这些键读，别改名）。
LAYER_CATALOG = "catalog"        # 1) Catalog 全量工具
LAYER_ELIGIBLE = "eligible"      # 2) 权限/场景过滤后
LAYER_RANKED = "ranked"          # 3) 排序后的候选
LAYER_FINAL = "final"            # 4) 最终传给模型的 tools


@dataclass(slots=True)
class ToolWindowSnapshot:
    """一次工具窗口构造的**四层证据**（只记名字/状态/原因，不含参数与正文）。"""

    scene: str = ""
    limit: int = 0
    catalog: list[str] = field(default_factory=list)
    eligible: list[str] = field(default_factory=list)
    ranked: list[str] = field(default_factory=list)
    final: list[str] = field(default_factory=list)
    #: 每一层相对上一层的**差集**（谁在哪一层消失的，一眼可见）。
    dropped_by_layer: dict[str, list[str]] = field(default_factory=dict)
    pinned: list[str] = field(default_factory=list)
    dropped_core: list[str] = field(default_factory=list)
    trimmed_optional: list[str] = field(default_factory=list)
    #: 三态可见性：``{name: catalog|eligible|available|unavailable}``。
    visibility: dict[str, str] = field(default_factory=dict)
    #: 触发本轮强制项的原因（``core`` / ``scope:write`` …），便于回溯"为什么它被钉住"。
    mandatory_reason: str = "core"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "limit": int(self.limit),
            "layers": {
                LAYER_CATALOG: list(self.catalog),
                LAYER_ELIGIBLE: list(self.eligible),
                LAYER_RANKED: list(self.ranked),
                LAYER_FINAL: list(self.final),
            },
            "counts": {
                LAYER_CATALOG: len(self.catalog),
                LAYER_ELIGIBLE: len(self.eligible),
                LAYER_RANKED: len(self.ranked),
                LAYER_FINAL: len(self.final),
            },
            "dropped_by_layer": {key: list(value) for key, value in self.dropped_by_layer.items()},
            "pinned": list(self.pinned),
            "dropped_core": list(self.dropped_core),
            "trimmed_optional": list(self.trimmed_optional),
            "visibility": dict(self.visibility),
            "mandatory_reason": self.mandatory_reason,
        }

    def log(self) -> None:
        """打一条结构化日志（排障时不用看代码就知道工具在哪一层消失）。"""
        logger.info(
            "[tool-window] scene={} limit={} catalog={} eligible={} ranked={} final={} "
            "pinned={} dropped_core={} trimmed={}",
            self.scene,
            self.limit,
            len(self.catalog),
            len(self.eligible),
            len(self.ranked),
            len(self.final),
            ",".join(self.pinned) or "-",
            ",".join(self.dropped_core) or "-",
            ",".join(self.trimmed_optional) or "-",
        )

    def diff(self, other: "ToolWindowSnapshot") -> list[str]:
        """两轮之间丢失的工具名（ReAct 每轮用它回答"上一轮还能用的工具这轮怎么没了"）。"""
        before, after = set(self.final), set(other.final)
        return sorted(before - after)


def build_snapshot(
    *,
    scene: str,
    limit: int,
    catalog: Sequence[Any] = (),
    eligible: Sequence[Any] = (),
    ranked: Sequence[Any] = (),
    final: Sequence[Any] = (),
    trim: TrimResult | None = None,
    mandatory_reason: str = "core",
) -> ToolWindowSnapshot:
    """把四层候选归一成快照（名字去重保序，差值自动算好）。"""

    def _names(rows: Sequence[Any]) -> list[str]:
        out: list[str] = []
        for item in rows or ():
            name = str(getattr(item, "name", "") or item or "")
            if name and name not in out:
                out.append(name)
        return out

    catalog_names = _names(catalog)
    eligible_names = _names(eligible)
    ranked_names = _names(ranked)
    final_names = _names(final)
    dropped: dict[str, list[str]] = {}
    # 逐层差集：上一层的工具在这一层不见了。
    for layer, previous, current in (
        ("catalog→eligible", catalog_names, eligible_names),
        ("eligible→ranked", eligible_names, ranked_names),
        ("ranked→final", ranked_names, final_names),
    ):
        gone = [name for name in previous if name not in set(current)]
        if gone:
            dropped[layer] = gone
    visibility = {
        str(getattr(item, "name", "")): visibility_state(item)
        for item in [*eligible, *final]
        if str(getattr(item, "name", "") or "")
    }
    return ToolWindowSnapshot(
        scene=str(scene or ""),
        limit=int(limit or 0),
        catalog=catalog_names,
        eligible=eligible_names,
        ranked=ranked_names,
        final=final_names,
        dropped_by_layer=dropped,
        pinned=list(trim.pinned) if trim else [],
        dropped_core=list(trim.dropped_core) if trim else [],
        trimmed_optional=list(trim.trimmed_optional) if trim else [],
        visibility=visibility,
        mandatory_reason=str(mandatory_reason or "core"),
    )


def apply_tool_window(
    capabilities: Sequence[Any],
    *,
    limit: int,
    scene: str = "",
    catalog: Sequence[Any] | None = None,
    eligible: Sequence[Any] | None = None,
    ranked: Sequence[Any] | None = None,
    extra_mandatory: Iterable[str] = (),
    mandatory_reason: str = "core",
    layer: str = "",
) -> tuple[list[Any], ToolWindowSnapshot]:
    """**唯一**的"上限内选工具"入口：保留强制工具 + 记录四层快照。

    所有 ``[:8]`` / ``[:3]`` 截断都应该改走这里。``layer`` 是调用点标签
    （``chat_graph.final`` / ``chat_graph.cache_merge`` / ``react.per_round`` …），
    日志里据此定位是**哪一处**截断丢的工具。

    返回 ``(最终工具列表, 快照)``；调用方可直接用快照打日志或塞进过程事件。
    """
    rows = list(capabilities or ())
    pinned = [*extra_mandatory]
    # 统一资源能力层（Phase 2）：候选池里有**变更类**工具时，同资源的读取入口必须
    # 一起留下（"新增 workspace_write 不会让 resource.read 消失"）。
    # 保护作用在**截断层**而不是窗口生成层——丢工具真正发生在这里。
    try:
        from app.agents.capabilities.resource_window import read_guards, window_enabled

        if window_enabled():
            guards = read_guards(rows)
            if guards:
                pinned = [*pinned, *sorted(guards)]
                mandatory_reason = f"{mandatory_reason}+resource.read"
    except Exception as exc:  # noqa: BLE001 - 保护规则失败不能影响注入
        logger.debug("[tool-window] 读取保护规则失败: {}", str(exc)[:120])
    trim = trim_with_mandatory(rows, limit=limit, extra_mandatory=pinned)
    snapshot = build_snapshot(
        scene=scene,
        limit=int(limit or 0),
        catalog=list(catalog) if catalog is not None else rows,
        eligible=list(eligible) if eligible is not None else rows,
        ranked=list(ranked) if ranked is not None else rows,
        final=trim.capabilities,
        trim=trim,
        mandatory_reason=mandatory_reason,
    )
    if layer:
        logger.info(
            "[tool-window] layer={} scene={} limit={} final={} pinned={} trimmed={} dropped_core={}",
            layer,
            scene,
            int(limit or 0),
            len(snapshot.final),
            ",".join(snapshot.pinned) or "-",
            ",".join(snapshot.trimmed_optional) or "-",
            ",".join(snapshot.dropped_core) or "-",
        )
    else:
        snapshot.log()
    # 返回**对象**（不是名字）：调用点还要继续读 capability 的字段构造工具定义。
    return list(trim.capabilities), snapshot


def registry_epoch() -> str:
    """工具注册表版本（方案 §五 P1：缓存失效不能只靠域策略版本）。

    旧实现里会话发现缓存的失效条件只有"工具域策略文件版本"。于是**插件安装/卸载、
    Provider 增删工具、工具 schema 升级**都不会让缓存失效，表现为：

    * 新工具在旧会话里不可见（要等 30 分钟 TTL 过期）；
    * 已下线工具继续留在会话缓存里；
    * schema 更新后仍用旧版本。

    这里把三种变化都折进一个版本串：

    * **影子注册表**（``ToolSpec`` 的数量 + 限定名 + 版本 + 输入 schema 摘要）——
      覆盖插件安装/卸载与 schema 升级；
    * **能力目录**（catalog 的限定名 + 契约版本）——覆盖"新增工具进入了能力系统"；
    * 两者都不可用时返回 ``unknown``（**保守**：调用方应视为"无法确认"，宁可重算）。

    实现只读进程内已加载的注册表，不做 IO，因此可以随时调用。
    """
    parts: list[str] = []
    try:
        from app.contracts.tools import export_tool_specs

        specs = export_tool_specs()
        rows = [
            f"{name}@{str((spec or {}).get('version') or '')}"
            f"#{_stable_hash(json.dumps((spec or {}).get('input_schema') or {}, sort_keys=True, default=str))}"
            for name, spec in sorted(specs.items())
        ]
        parts.append(f"specs:{len(rows)}:{_stable_hash('|'.join(rows))}")
    except Exception as exc:  # noqa: BLE001 - 注册表不可读时退化为"未知"
        logger.debug("[tool-registry] ToolSpec 摘要不可用: {}", str(exc)[:120])
    try:
        from app.agents.capabilities.catalog import capability_catalog

        names = sorted(
            f"{item.qualified_name}" for item in capability_catalog.all()
        )
        parts.append(f"catalog:{len(names)}:{_stable_hash('|'.join(names))}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool-registry] 能力目录摘要不可用: {}", str(exc)[:120])
    if not parts:
        return "unknown"
    return _stable_hash(";".join(parts))


def _stable_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


__all__ = [
    "CORE_TOOLS",
    "LAYER_CATALOG",
    "LAYER_ELIGIBLE",
    "LAYER_FINAL",
    "LAYER_RANKED",
    "STATE_AVAILABLE",
    "STATE_CATALOG",
    "STATE_ELIGIBLE",
    "STATE_UNAVAILABLE",
    "ToolWindowSnapshot",
    "TrimResult",
    "apply_tool_window",
    "build_snapshot",
    "is_mandatory",
    "pin_tools",
    "registry_epoch",
    "tool_identity",
    "trim_with_mandatory",
    "visibility_state",
]
