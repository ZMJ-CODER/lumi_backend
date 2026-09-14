"""内部超时阶梯回归：无界等待 → 有界、可调、按复杂度升级。

背景：走真实 ``submit_job`` 的用例曾整体挂死（90s 硬杀），定位到计划编译期
``plan_compiler.build_capability_snapshot`` → MCP ``list_tools`` →
``_McpSessionWorker.call`` 的 future 永不 set：桌面客户端不在线时没有任何 deadline，
一次提交永远不返回。

本文件锁定四件事：

1. 阶梯取值：M0/M1/M2/M3 → 5/10/30/60（默认），可用 ``settings`` 调整；
2. 映射与优先级：显式 override > 模型计划预算 > 复杂度档位 > 节点数兜底 > M1；
3. 有界等待真的会超时（``enforce`` 抛 ``DeadlineExceeded``，是
   ``asyncio.TimeoutError`` 子类，旧捕获点继续有效），且会取消被等待的协程；
4. 开关关闭时仍然是**有界**的（绝不退回无界），只是放宽到阶梯最大值。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.orchestration.runtime import timeout_ladder as ladder
from app.core.config import settings


def _reset_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_LADDER_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_M0_SECONDS", 5)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_M1_SECONDS", 10)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_M2_SECONDS", 30)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_M3_SECONDS", 60)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_NODE_TIERS", "3,6,12")


# ── (1) 阶梯默认值 ────────────────────────────────────────────────


def test_ladder_defaults_are_five_ten_thirty_sixty(monkeypatch):
    _reset_settings(monkeypatch)
    assert ladder_defaults() == {"m0": 5.0, "m1": 10.0, "m2": 30.0, "m3": 60.0}
    # 未知档位按 M1，绝不返回 0/None（0 等于无界）。
    assert ladder.tier_timeout_seconds("") == 10.0
    assert ladder.tier_timeout_seconds("m9") == 10.0
    for tier in ladder.TIERS:
        assert ladder.tier_timeout_seconds(tier) > 0


def ladder_defaults() -> dict[str, float]:
    return {tier: ladder.tier_timeout_seconds(tier) for tier in ladder.TIERS}


def test_ladder_values_follow_settings(monkeypatch):
    _reset_settings(monkeypatch)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_M2_SECONDS", 42)
    assert ladder.tier_timeout_seconds("m2") == 42.0


# ── (2) 映射与优先级 ─────────────────────────────────────────────


def test_tier_resolution_prefers_explicit_then_plan_size(monkeypatch):
    _reset_settings(monkeypatch)
    # 显式档位优先（大小写/历史写法都收敛）
    assert ladder.effective_tier(tier="M3", node_count=1) == "m3"
    assert ladder.effective_tier(tier="complexity=m2") == "m2"
    # 没有档位时按计划节点数兜底：≤3→M0，≤6→M1，≤12→M2，其余 M3
    assert [ladder.tier_for_node_count(n) for n in (0, 3, 4, 6, 7, 12, 13)] == [
        "m0", "m0", "m1", "m1", "m2", "m2", "m3",
    ]
    assert ladder.effective_tier(node_count=7) == "m2"
    # 都没有 → M1（既不激进也不放任）
    assert ladder.effective_tier() == "m1"


def test_override_beats_plan_budget_beats_tier(monkeypatch):
    _reset_settings(monkeypatch)
    # 用户/调用方覆盖最高
    assert ladder.effective_timeout_seconds(tier="m0", override=42) == 42.0
    # 其次按模型计划自报预算
    assert ladder.effective_timeout_seconds(tier="m0", plan_seconds=17) == 17.0
    # 再其次才是档位
    assert ladder.effective_timeout_seconds(tier="m2") == 30.0
    # 自定义计划预算受合法区间夹取：可调 ≠ 可关
    assert ladder.effective_timeout_seconds(tier="m0", override=0) == 5.0
    assert ladder.effective_timeout_seconds(tier="m0", override=-3) == 5.0
    assert ladder.effective_timeout_seconds(tier="m3", override=10_000) == (
        ladder.MAX_OVERRIDE_SECONDS
    )


def test_budget_description_is_auditable(monkeypatch):
    _reset_settings(monkeypatch)
    budget = ladder.describe_budget(tier="m2", node_count=4)
    assert budget["tier"] == "m2"
    assert budget["seconds"] == 30.0
    assert budget["source"] == "ladder"
    assert budget["ladder"] == {"m0": 5.0, "m1": 10.0, "m2": 30.0, "m3": 60.0}
    assert ladder.describe_budget(tier="m1", override=7)["source"] == "override"
    assert ladder.describe_budget(tier="m1", plan_seconds=7)["source"] == "plan"
    assert budget["ladder_enabled"] is True


# ── (3)(4) 有界等待 ──────────────────────────────────────────────


def test_enforce_times_out_and_cancels_inner_wait(monkeypatch):
    _reset_settings(monkeypatch)
    cancelled = asyncio.Event()

    async def never() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def scenario():
        with pytest.raises(ladder.DeadlineExceeded) as excinfo:
            await ladder.enforce(never(), seconds=0.05, label="测试等待", tier="m0")
        # 是 TimeoutError 子类：既有 ``except asyncio.TimeoutError`` 捕获点继续有效
        assert isinstance(excinfo.value, asyncio.TimeoutError)
        assert excinfo.value.timeout == 0.05
        assert "测试等待" in str(excinfo.value) and "m0" in str(excinfo.value)
        # 超时必须取消被等待的协程，不能把事件循环留在半死状态
        await asyncio.wait_for(cancelled.wait(), timeout=1)

    asyncio.run(scenario())


def test_enforce_passes_result_through_without_timeout(monkeypatch):
    _reset_settings(monkeypatch)

    async def fast() -> str:
        return "ok"

    async def scenario():
        assert await ladder.enforce(fast(), seconds=5, label="x") == "ok"
        # 没有上界（0/None）时按调用方既有语义直接等待，不抛错
        assert await ladder.enforce(fast(), seconds=0, label="x") == "ok"

    asyncio.run(scenario())


def test_ladder_disabled_stays_bounded(monkeypatch):
    _reset_settings(monkeypatch)
    monkeypatch.setattr(settings, "AGENT_TIMEOUT_LADDER_ENABLED", False)
    # 关闭阶梯只是放宽，绝不返回无界值
    assert ladder.effective_timeout_seconds(tier="m0") == 60.0
    assert ladder.effective_timeout_seconds(tier="m0", override=7) == 60.0
    assert ladder.describe_budget(tier="m0")["ladder_enabled"] is False
