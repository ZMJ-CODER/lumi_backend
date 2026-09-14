"""聚焦用例的内部超时：把"挂住"变成"可见的红"。

背景：``tests/test_plan_first_resume_enablement.py`` 与
``tests/test_agent_process_log_persistence.py`` 里几个走真实 ``submit_job`` 的用例，
曾因为内部等待无界（计划编译期 MCP ``list_tools`` 握手的 future 永不 set）**挂死**，
只能靠外层 90s 硬杀，看不出是哪一步卡住。生产侧已按超时阶梯把该等待有界化；这里
再加一层**用例级**保险：给可能挂住的场景套一个可调 deadline，超时直接失败并报出
是哪个标签卡住，而不是把整个测试会话钉死。

可调：默认值取自 ``app.agents.orchestration.runtime.timeout_ladder``（M0/M1/M2/M3 →
5/10/30/60 秒，配置可改、请求可覆盖）；调用方可显式传 ``seconds`` 或 ``tier``。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")

# 这些用例里被等待的是"本地假实现 + 可选 MCP 探测"，属于单次外部往返量级。
# 注意：走真实 ``submit_job`` 的场景在**没有桌面客户端**的环境里会先撞上计划编译期
# 的 MCP 能力发现超时（``AGENT_MCP_DISCOVERY_TIMEOUT_SECONDS``，默认 5s）再降级，
# 因此那类场景要显式用 M1（10s）档，M0（5s）会恰好卡在降级前误报。
DEFAULT_TEST_TIER = "m0"


def _ladder():
    from app.agents.orchestration.runtime import timeout_ladder

    return timeout_ladder


def test_deadline_seconds(
    *,
    tier: str = DEFAULT_TEST_TIER,
    node_count: Any = None,
    override: Any = None,
) -> float:
    """本用例场景的 deadline 秒数（可调：tier / override）。"""
    return _ladder().effective_timeout_seconds(
        tier=tier, node_count=node_count, override=override, fallback=DEFAULT_TEST_TIER
    )


async def with_deadline(
    awaitable: Awaitable[T],
    *,
    label: str,
    tier: str = DEFAULT_TEST_TIER,
    node_count: Any = None,
    override: Any = None,
) -> T:
    """在 deadline 内等待 ``awaitable``；超时抛 AssertionError（带标签，便于定位）。

    超时会取消被等待的协程（``wait_for`` 语义），不会把事件循环留在半死状态。
    """
    seconds = test_deadline_seconds(tier=tier, node_count=node_count, override=override)
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError as exc:  # noqa: PERF203 - 单个等待点
        raise AssertionError(
            f"用例内部超时：{label} 在 {seconds:g}s 内未完成（档位 {tier}）；"
            "说明有内部等待没有上界，请查 timeout_ladder 的接入点"
        ) from exc


__all__ = ["DEFAULT_TEST_TIER", "test_deadline_seconds", "with_deadline"]
