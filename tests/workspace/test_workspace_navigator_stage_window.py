"""ReAct 工具窗口的读取域收敛回归测试（阶段化能力注入）。

产品方案第 1、6 条：前端内部 MCP 工具可以很多，但真正发给模型的读取工具
必须只有一个聚合入口。这里验证 ReAct 阶段注入不会再带回内部原子读取名。
"""

from __future__ import annotations

import asyncio

from app.agents.orchestration.react_runner import OfficeReactRunner


def _runner() -> OfficeReactRunner:
    return OfficeReactRunner(
        user_id="u1",
        job_id="job-1",
        user_role="user",
        workspace_id="w1",
        workspace_summary="工作区已注册且可访问。",
    )


def _fake_caps(monkeypatch, *, group_names):
    calls: list[frozenset[str]] = []

    async def fake_group(user_id, scene, user_role, workspace_id, allowed_raw):
        calls.append(frozenset(allowed_raw))
        return [type("C", (), {"name": f"mcp__lumi_pc__{name}", "raw_name": name})() for name in group_names]

    monkeypatch.setattr(
        "app.agents.skills.executor.get_workspace_action_capabilities", fake_group
    )
    return calls


def test_read_step_only_injects_aggregated_navigator(monkeypatch):
    calls = _fake_caps(monkeypatch, group_names=("workspace_navigator",))
    runner = _runner()
    result = asyncio.run(runner._maybe_inject_workspace_stage_window([], "读取工作区里的 README 文件"))
    names = {item.name for item in result}
    assert names == {"mcp__lumi_pc__workspace_navigator"}
    # 读取域请求的就是聚合入口本身，而不是 catalog/list/read/search 组合。
    assert frozenset({"workspace_navigator"}) in calls
    for leaked in ("workspace_list", "workspace_read", "workspace_catalog", "workspace_search"):
        assert all(leaked not in group for group in calls)


def test_modify_step_injects_atomic_operation_tools(monkeypatch):
    """写阶段必须把**四个原子操作工具**注入候选窗（用户报的"模型没有写入能力"）。"""
    from app.workspace.context import (
        WORKSPACE_OPERATION_CAPABILITIES,
        WORKSPACE_STAGE_WRITE_CAPABILITIES,
    )

    requested: list[frozenset[str]] = []

    async def fake_group(user_id, scene, user_role, workspace_id, allowed_raw):
        requested.append(frozenset(allowed_raw))
        if frozenset(allowed_raw) == frozenset({"workspace_navigator"}):
            return [type("C", (), {"name": "mcp__lumi_pc__workspace_navigator"})()]
        if frozenset(allowed_raw) == WORKSPACE_OPERATION_CAPABILITIES:
            return [
                type("C", (), {"name": f"mcp__lumi_pc__{name}"})()
                for name in sorted(WORKSPACE_OPERATION_CAPABILITIES)
            ]
        return []

    monkeypatch.setattr(
        "app.agents.skills.executor.get_workspace_action_capabilities", fake_group
    )
    runner = _runner()
    result = asyncio.run(
        runner._maybe_inject_workspace_stage_window([], "修改 src 下的代码并写入新文件")
    )
    names = {item.name for item in result}
    assert "mcp__lumi_pc__workspace_navigator" in names
    for tool in ("workspace_write", "workspace_edit", "workspace_move", "workspace_delete"):
        assert f"mcp__lumi_pc__{tool}" in names, f"写阶段缺少 {tool}"
    # 有原子操作工具时不再注入旧的暂存对（窗口更小、语义只有一套）
    assert not WORKSPACE_STAGE_WRITE_CAPABILITIES & {
        item.raw_name for item in result if hasattr(item, "raw_name")
    }
    assert not {"mcp__lumi_pc__workspace_list", "mcp__lumi_pc__workspace_read"} & names


def test_modify_step_falls_back_to_staged_tools_for_old_client(monkeypatch):
    """老客户端没有原子操作工具时，退回暂存对（不出现"没有写工具"的空窗）。"""
    from app.workspace.context import (
        WORKSPACE_OPERATION_CAPABILITIES,
        WORKSPACE_STAGE_WRITE_CAPABILITIES,
    )

    async def fake_group(user_id, scene, user_role, workspace_id, allowed_raw):
        group = frozenset(allowed_raw)
        if group == frozenset({"workspace_navigator"}):
            return [type("C", (), {"name": "mcp__lumi_pc__workspace_navigator"})()]
        if group == WORKSPACE_OPERATION_CAPABILITIES:
            return []
        if group == WORKSPACE_STAGE_WRITE_CAPABILITIES:
            return [type("C", (), {"name": f"mcp__lumi_pc__{name}"})() for name in group]
        return []

    monkeypatch.setattr(
        "app.agents.skills.executor.get_workspace_action_capabilities", fake_group
    )
    runner = _runner()
    result = asyncio.run(
        runner._maybe_inject_workspace_stage_window([], "修改 src 下的代码并写入新文件")
    )
    names = {item.name for item in result}
    assert "mcp__lumi_pc__workspace_navigator" in names
    assert "mcp__lumi_pc__workspace_stage_write" in names


def test_no_workspace_vocab_keeps_window_untouched(monkeypatch):
    _fake_caps(monkeypatch, group_names=("workspace_navigator",))
    runner = _runner()
    existing = [type("C", (), {"name": "web_search"})()]
    result = asyncio.run(runner._maybe_inject_workspace_stage_window(existing, "今天天气怎么样"))
    assert [item.name for item in result] == ["web_search"]


def test_navigator_is_classified_as_read_tool():
    assert OfficeReactRunner._is_read_tool("workspace_navigator")
    assert OfficeReactRunner._is_read_tool("workspace_read")
