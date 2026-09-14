"""审批档位：副作用/声明 → ``auto`` / ``routine`` / ``critical``（**纯决策**）。

从 ``app/agents/capabilities/catalog/tool_registry.py`` 抽出（结构重构 P3 第一批）。
这是"安全边界"的纯核：给定"这个能力会做什么"和"它自述了什么"，算出该不该人工点头。

三条不变量（原实现如此，抽包后逐字保留）：

1. **什么都不声明就不猜**：一个副作用都没声明时返回 ``""``，由调用方走保守默认，
   而不是默认成 ``auto``（那等于把未知当安全）；
2. **"本机必须确认"只抬不降**：``auto`` 会被抬到 ``routine``——A 档会在真实写入前静默执行，
   与"本机要确认"直接矛盾；
3. **自述只能收紧**（由调用方用 :func:`stricter` 合并），不能放宽可信基线。

**表是参数，不是常量**：契约词表（``SideEffectKind``：``read/write/delete/execute/network/external``）
的映射由本模块给出（:data:`SIDE_EFFECT_TIER`），应用侧可以叠加自己的合成副作用名
（本项目有 ``publish``），通过 ``table=`` 传入——这样"哪些副作用算危险"这件事
既能被第二个服务复用，又允许应用保留自己的补充。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

TIER_AUTO = "auto"
TIER_ROUTINE = "routine"
TIER_CRITICAL = "critical"

#: 档位强弱顺序：``critical > routine > auto``（``""`` = 没有意见）。
TIER_ORDER: Mapping[str, int] = {TIER_AUTO: 0, TIER_ROUTINE: 1, TIER_CRITICAL: 2}

#: 副作用 → 档位。取值必须覆盖契约的 ``SideEffectKind``：
#: 漏一个就会掉进"默认 routine"，把 ``external``（对外发布，不可逆）误判成普通写入。
SIDE_EFFECT_TIER: Mapping[str, str] = {
    "read": TIER_AUTO,
    "write": TIER_ROUTINE,
    "execute": TIER_ROUTINE,
    "delete": TIER_ROUTINE,
    # 网络访问本身不可逆性有限，但会把数据带出本机 → 至少例行确认。
    "network": TIER_ROUTINE,
    # 对外发布/发送：收不回来，按始终确认。
    "external": TIER_CRITICAL,
}


def side_effect_tier(effects: Any, *, table: Mapping[str, str] | None = None) -> str:
    """副作用集合 → 档位（``""`` = 一个副作用都没声明，不猜）。

    ``effects`` 可以是 ``SideEffectKind`` 枚举、字符串或它们的混合——两种词汇
    （契约枚举与应用侧的合成名）都要认。
    """
    lookup = SIDE_EFFECT_TIER if table is None else table
    names = {str(getattr(item, "value", item) or "").strip() for item in effects or ()}
    names.discard("")
    if not names:
        return ""
    tiers = {lookup.get(name, TIER_ROUTINE) for name in names}
    if TIER_CRITICAL in tiers:
        return TIER_CRITICAL
    if TIER_ROUTINE in tiers:
        return TIER_ROUTINE
    return TIER_AUTO


def stricter(first: str, second: str) -> str:
    """取更严的一档（``""`` = 没有意见）。``critical > routine > auto``。"""
    candidates = [item for item in (str(first or ""), str(second or "")) if item in TIER_ORDER]
    if not candidates:
        return ""
    return max(candidates, key=lambda item: TIER_ORDER[item])


def normalize_tier(value: Any) -> str:
    """非法/空值 → ``""``（视为未声明），而不是硬塞一个档位。"""
    text = str(value or "").strip().casefold()
    return text if text in TIER_ORDER else ""


def floor_for_local_confirmation(tier: str, needs_local: bool) -> str:
    """"本机必须再确认一次" → 至少 routine 档（**只抬不降**）。"""
    if needs_local and tier == TIER_AUTO:
        return TIER_ROUTINE
    return tier


def manifest_requires_local_confirmation(manifest: Any) -> bool:
    """Manifest 是否要求人工确认（副作用命中审批清单，或任一权限要求本机确认）。

    异常一律当作"需要确认"：自述属性读不出来，不能变成"无需确认"。
    """
    try:
        if bool(getattr(manifest, "needs_approval", False)):
            return True
    except Exception:  # noqa: BLE001 - 自述属性异常不能变成"无需确认"
        return True
    for item in getattr(manifest, "permissions", None) or ():
        if bool(getattr(item, "needs_local_confirmation", False)):
            return True
    return False


def manifest_tier_of(manifest: Any, *, table: Mapping[str, str] | None = None) -> str:
    """``PluginManifest`` 的声明 → 档位（副作用 + 权限里的本机确认）。

    装到哪一侧、跑什么运行时都不影响档位——那是租约与运行方式的事。
    """
    tier = side_effect_tier(getattr(manifest, "side_effects", None), table=table)
    if not tier:
        return ""
    return floor_for_local_confirmation(tier, manifest_requires_local_confirmation(manifest))


def descriptor_declared_tier(descriptor: Any, *, table: Mapping[str, str] | None = None) -> str:
    """``CapabilityDescriptor`` 的声明 → 档位（副作用 + "本机需确认"）。

    与 :func:`manifest_tier_of` 同构：描述符声明一次，新能力就自动有档位，
    不必回头改审批词表。
    """
    tier = side_effect_tier(getattr(descriptor, "side_effects", None), table=table)
    if not tier:
        return ""
    return floor_for_local_confirmation(
        tier, bool(getattr(descriptor, "needs_local_confirmation", False))
    )


def merge_declared(baseline: str, declared: Iterable[str]) -> str:
    """可信基线 + 若干自述 → 生效档位（**自述只能收紧**，没有基线时自述即答案）。

    这是"插件声明一次就生效"与"不可信声明不能放宽安全"两条要求的交点。
    """
    result = normalize_tier(baseline)
    for item in declared:
        result = stricter(result, normalize_tier(item))
    return result


__all__ = [
    "SIDE_EFFECT_TIER",
    "TIER_AUTO",
    "TIER_CRITICAL",
    "TIER_ORDER",
    "TIER_ROUTINE",
    "descriptor_declared_tier",
    "floor_for_local_confirmation",
    "manifest_requires_local_confirmation",
    "manifest_tier_of",
    "merge_declared",
    "normalize_tier",
    "side_effect_tier",
    "stricter",
]
