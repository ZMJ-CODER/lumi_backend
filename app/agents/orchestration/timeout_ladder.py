"""内部执行超时阶梯：无界等待 → 有界、可调、可按复杂度升级。

**为什么存在**：计划编译期需要向桌面客户端（Electron/MCP）发现能力、工作区上下文
需要探测设备健康，这些等待一旦没有 deadline，一次 ``submit_job`` 就可能永不返回
（实测：``plan_compiler.build_capability_snapshot`` → MCP ``list_tools`` 卡在
``_McpSessionWorker.call`` 的 future 上，事件循环空转、无任何任务在跑）。测试里的
表现就是"用例挂住"，生产里的表现就是"提交一直转圈"。

**阶梯**（默认值，全部来自 ``settings``，可改）：

===========================  ======  ==================================
档位                          默认    适用
===========================  ======  ==================================
M0（单步/直答）                5s     一次外部往返或纯文本
M1（单次只读/原子动作）        10s     一次读取 + 回答
M2（多步/依赖）                30s     数步串并行
M3（动态/ReAct/长任务）        60s     规划-执行-再规划
===========================  ======  ==================================

**优先级**（高 → 低）：

1. 显式覆盖 ``override``（请求里的 ``timeout_seconds``，或调用方已知的更准预算）；
2. 模型计划自报的预算 ``plan_seconds``（计划里带了就按计划的，最小不低于档位值）；
3. 复杂度档位（``ComplexityLevel``，或按计划节点数兜底映射）。

**边界**：本模块只做"取值 + 有界等待"，不做取消语义、不落库、不发事件；调用方拿到
``DeadlineExceeded`` 后按既有降级路径处理（能力发现失败→工具集为空、健康探测失败
→设备离线）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, TypeVar

from app.core.config import settings

T = TypeVar("T")

# 复杂度档位词表（与 ``tca.ComplexityLevel`` 的值一致：m0/m1/m2/m3）。
TIERS: tuple[str, ...] = ("m0", "m1", "m2", "m3")

# 档位 → settings 属性名。缺省档位是 M1：既不是"一次直答"，也不是"长任务"。
_TIER_SETTINGS: dict[str, str] = {
    "m0": "AGENT_TIMEOUT_M0_SECONDS",
    "m1": "AGENT_TIMEOUT_M1_SECONDS",
    "m2": "AGENT_TIMEOUT_M2_SECONDS",
    "m3": "AGENT_TIMEOUT_M3_SECONDS",
}
_FALLBACK_SECONDS: dict[str, float] = {"m0": 5.0, "m1": 10.0, "m2": 30.0, "m3": 60.0}
_DEFAULT_TIER = "m1"
_DEFAULT_NODE_TIERS: tuple[int, int, int] = (3, 6, 12)

# 覆盖值的合法区间：过大等于无界等待，过小会让正常任务误判失败。
MIN_OVERRIDE_SECONDS = 1.0
MAX_OVERRIDE_SECONDS = 600.0


class DeadlineExceeded(TimeoutError):
    """有界等待超时（``asyncio.TimeoutError`` 的子类，旧捕获点继续有效）。"""

    def __init__(self, label: str, timeout: float, tier: str = "") -> None:
        self.label = str(label or "")
        self.timeout = float(timeout)
        self.tier = str(tier or "")
        suffix = f"（档位 {self.tier}）" if self.tier else ""
        super().__init__(f"{self.label or '内部等待'}超时：{self.timeout:g}s{suffix}")


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def tier_timeout_seconds(tier: str = "") -> float:
    """档位 → 秒（未知/空档位按 M1；settings 缺失时用内置兜底值）。"""
    key = str(getattr(tier, "value", tier) or "").strip().casefold()
    if key not in _TIER_SETTINGS:
        key = _DEFAULT_TIER
    configured = _positive(getattr(settings, _TIER_SETTINGS[key], None))
    return configured if configured is not None else _FALLBACK_SECONDS[key]


def _node_tier_bounds() -> tuple[int, int, int]:
    raw = str(getattr(settings, "AGENT_TIMEOUT_NODE_TIERS", "") or "")
    parsed: list[int] = []
    for piece in raw.split(","):
        try:
            number = int(str(piece).strip())
        except (TypeError, ValueError):
            continue
        if number > 0:
            parsed.append(number)
    if len(parsed) != 3 or parsed != sorted(parsed):
        return _DEFAULT_NODE_TIERS
    return parsed[0], parsed[1], parsed[2]


def tier_for_node_count(node_count: Any) -> str:
    """计划节点数 → 档位兜底映射（没有 TCA 档位时使用）。"""
    try:
        count = int(node_count or 0)
    except (TypeError, ValueError):
        count = 0
    first, second, third = _node_tier_bounds()
    if count <= first:
        return "m0"
    if count <= second:
        return "m1"
    if count <= third:
        return "m2"
    return "m3"


def _normalize_tier(value: Any) -> str:
    key = str(getattr(value, "value", value) or "").strip().casefold()
    if key in _TIER_SETTINGS:
        return key
    # 兼容 "M2"/"m2 "/"complexity=m2" 之类的历史写法。
    for tier in TIERS:
        if tier in key:
            return tier
    return ""


def effective_tier(*, tier: Any = "", node_count: Any = None, fallback: str = "") -> str:
    """取生效档位：显式档位 > 节点数映射 > 兜底档位 > M1。"""
    for candidate in (_normalize_tier(tier),):
        if candidate:
            return candidate
    if node_count is not None:
        return tier_for_node_count(node_count)
    return _normalize_tier(fallback) or _DEFAULT_TIER


def effective_timeout_seconds(
    *,
    tier: Any = "",
    node_count: Any = None,
    override: Any = None,
    plan_seconds: Any = None,
    fallback: str = "",
) -> float:
    """解析本次内部等待的秒数（见模块 docstring 的优先级）。

    ``override`` 会被夹到 ``[MIN_OVERRIDE_SECONDS, MAX_OVERRIDE_SECONDS]``：这是
    "可调"而不是"可关"，避免一次手滑把内部等待变回无界。
    """
    ladder_enabled = bool(getattr(settings, "AGENT_TIMEOUT_LADDER_ENABLED", True))
    resolved_tier = effective_tier(tier=tier, node_count=node_count, fallback=fallback)
    base = tier_timeout_seconds(resolved_tier)
    if not ladder_enabled:
        # 阶梯关闭时退回档位自带的最大值，仍然有界（绝不返回 0/None）。
        return max(base, _FALLBACK_SECONDS["m3"])
    for candidate in (_positive(override), _positive(plan_seconds)):
        if candidate is None:
            continue
        return min(max(candidate, MIN_OVERRIDE_SECONDS), MAX_OVERRIDE_SECONDS)
    return base


def describe_budget(
    *,
    tier: Any = "",
    node_count: Any = None,
    override: Any = None,
    plan_seconds: Any = None,
    fallback: str = "",
) -> dict[str, Any]:
    """可审计的预算描述（写进 ``routing["timeout"]``，便于排查"为什么这么快超时"）。"""
    resolved_tier = effective_tier(tier=tier, node_count=node_count, fallback=fallback)
    seconds = effective_timeout_seconds(
        tier=resolved_tier, node_count=node_count, override=override,
        plan_seconds=plan_seconds, fallback=fallback,
    )
    source = "override" if _positive(override) else (
        "plan" if _positive(plan_seconds) else "ladder"
    )
    return {
        "tier": resolved_tier,
        "seconds": seconds,
        "source": source,
        "ladder_enabled": bool(getattr(settings, "AGENT_TIMEOUT_LADDER_ENABLED", True)),
        "ladder": {tier: tier_timeout_seconds(tier) for tier in TIERS},
    }


async def enforce(
    awaitable: Awaitable[T],
    *,
    seconds: float,
    label: str = "",
    tier: str = "",
) -> T:
    """有界等待：超时抛 ``DeadlineExceeded``（``asyncio.TimeoutError`` 子类）。

    超时按标准 ``wait_for`` 语义取消被等待的协程，调用方的既有降级路径不变。
    """
    timeout = _positive(seconds)
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:  # noqa: PERF203 - 单个等待点
        raise DeadlineExceeded(label, timeout, tier) from exc


__all__ = [
    "DeadlineExceeded",
    "MAX_OVERRIDE_SECONDS",
    "MIN_OVERRIDE_SECONDS",
    "TIERS",
    "describe_budget",
    "effective_tier",
    "effective_timeout_seconds",
    "enforce",
    "tier_for_node_count",
    "tier_timeout_seconds",
]
