"""能力/工具的**可见性状态机**（纯决策）。

从 ``app/agents/capabilities/catalog/resource.py`` 抽出（结构重构 P3 第三批）。
五个状态回答的是"这个东西现在处于哪一步"，而不是"能不能用"：

============================  ================================================
状态                          含义
============================  ================================================
``unregistered``              系统**不认识**这个工具（没有能力绑定）
``registered``                认识，但这一轮不在候选池里（或没探测到池状态）
``visible``                   在候选池里（模型/编排本轮看得见）
``available``                 池明确判定可用
``unavailable``               池明确判定不可用（被策略/权限/Provider 挡住）
============================  ================================================

三条不变量（原实现如此，抽包后逐字保留）：

1. **不认识 ≠ 允许**：没有绑定一律 ``unregistered``。把"系统不认识"读成"允许使用"
   是最危险的一类误读，所以这里**绝不复用**池的 ``eligible``；
2. **探测失败不谎报可用**：池状态读不出来时回落 ``registered``，而不是乐观地给
   ``visible``/``available``；
3. **状态只能由事实推出**：调用方给什么池状态就映什么，这里不猜。

池自己的状态词表（``eligible`` / ``catalog`` / …）是**应用概念**，通过
:data:`VisibilityVocabulary` 注入，因此这个状态机不认识任何具体应用的池。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

STATE_UNREGISTERED = "unregistered"
STATE_REGISTERED = "registered"
STATE_VISIBLE = "visible"
STATE_AVAILABLE = "available"
STATE_UNAVAILABLE = "unavailable"

#: 全部可见性状态（对外契约：前端/管理端按这个集合渲染）。
VISIBILITY_STATES: frozenset[str] = frozenset(
    {STATE_UNREGISTERED, STATE_REGISTERED, STATE_VISIBLE, STATE_AVAILABLE, STATE_UNAVAILABLE}
)


@dataclass(frozen=True, slots=True)
class VisibilityVocabulary:
    """池状态 → 可见性状态的映射词表（应用提供）。

    ``visible`` 是**集合**：不同调用方会用不同的名字表示"在池子里"
    （本项目有 ``eligible`` 与 ``catalog`` 两个），它们都映射到同一个对外状态。
    """

    available: frozenset[str] = frozenset({"available"})
    unavailable: frozenset[str] = frozenset({"unavailable"})
    visible: frozenset[str] = frozenset({"eligible", "catalog"})


DEFAULT_VISIBILITY_VOCABULARY = VisibilityVocabulary()


def resolve_visibility(
    *,
    known: bool,
    pool_state: str | None = None,
    vocabulary: VisibilityVocabulary | None = None,
) -> str:
    """由**事实**推出可见性状态。

    ``known=False`` → ``unregistered``（不认识的工具绝不当"可用"）；
    ``pool_state=None`` → ``registered``（调用方没探测，或探测失败）；
    其余按 ``vocabulary`` 映射，未命中的池状态一律回落 ``registered``。
    """
    if not known:
        return STATE_UNREGISTERED
    if pool_state is None:
        return STATE_REGISTERED
    words = vocabulary or DEFAULT_VISIBILITY_VOCABULARY
    state = str(pool_state or "").strip()
    if state in words.available:
        return STATE_AVAILABLE
    if state in words.unavailable:
        return STATE_UNAVAILABLE
    if state in words.visible:
        return STATE_VISIBLE
    return STATE_REGISTERED


def visibility_rank(state: str) -> int:
    """状态的"可见程度"排序（展示/断言用；未知状态排最低）。

    ``unavailable`` 与 ``registered`` 同为 1：**都表示"模型看不到"**，
    区分它们只对排障有意义，不该影响排序结果。
    """
    return {
        STATE_UNREGISTERED: 0,
        STATE_REGISTERED: 1,
        STATE_UNAVAILABLE: 1,
        STATE_VISIBLE: 2,
        STATE_AVAILABLE: 3,
    }.get(str(state or ""), 0)


def visibility_ranked(states: Mapping[str, str]) -> list[tuple[str, str]]:
    """``{名字: 状态}`` → 按可见程度降序、同档按名字升序的列表（稳定的展示顺序）。"""
    return sorted(states.items(), key=lambda item: (-visibility_rank(item[1]), item[0]))


__all__ = [
    "DEFAULT_VISIBILITY_VOCABULARY",
    "STATE_AVAILABLE",
    "STATE_REGISTERED",
    "STATE_UNAVAILABLE",
    "STATE_UNREGISTERED",
    "STATE_VISIBLE",
    "VISIBILITY_STATES",
    "VisibilityVocabulary",
    "resolve_visibility",
    "visibility_rank",
    "visibility_ranked",
]
