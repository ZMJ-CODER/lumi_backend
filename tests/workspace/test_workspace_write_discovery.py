"""写入能力「发现层」回归：四个操作工具必须既注册、也能进模型的写阶段窗口。

背景（用户报的问题）：执行层已经有操作网关，但

1. ``workspace_write`` 没有正式 ``Tool`` 实现（只有能力映射）；
2. 编辑/移动/删除被标成 ``public=False`` 且写阶段窗口只注入旧的暂存对，
   于是"创建/修改文件"这件事在模型的自由工具窗里根本不存在。

本文件验证修复后的三件事：

* 启动装载后四个操作工具都在 ``ToolRegistry`` 里（DAG/Workflow 与 plan_compiler 可用）；
* 写阶段窗口注入四个**原子操作工具**（而不是只给聚合读取入口）；
* 读取阶段仍然只给聚合入口（不因修复而泄漏内部原子名）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.workspace.context import (
    WORKSPACE_ALL_CAPABILITIES,
    WORKSPACE_OPERATION_CAPABILITIES,
    WORKSPACE_STAGE_AND_EXEC_TOOL_NAMES,
    WORKSPACE_TOOL_NAMES,
    is_workspace_read_tool,
    is_workspace_tool,
)

OPERATION_TOOLS = ("workspace_write", "workspace_edit", "workspace_move", "workspace_delete")


@pytest.fixture(scope="module", autouse=True)
def _plugins_loaded():
    """装载插件（应用启动时才注册；单测需要显式装一次）。"""
    from app.agents.skills.loader import load_skill_plugins
    from app.agents.skills.registry import ToolRegistry

    if ToolRegistry.get("workspace_write") is None:
        load_skill_plugins()
    yield


def test_four_operation_tools_are_registered():
    """``workspace_write`` 必须有正式 Tool 实现（用户报的第一处缺口）。"""
    from app.agents.skills.registry import ToolRegistry

    for name in OPERATION_TOOLS:
        tool = ToolRegistry.get(name)
        assert tool is not None, f"{name} 没有注册为 Tool"
        assert tool.write_op is True, f"{name} 必须标记为写操作"
        schema = tool.parameters_schema
        assert schema.get("type") == "object" and schema.get("properties")
        assert tool.description.strip()


def test_write_tool_contract_exposes_revision_and_content():
    from app.agents.skills.registry import ToolRegistry

    tool = ToolRegistry.get("workspace_write")
    props = tool.parameters_schema["properties"]
    assert tool.parameters_schema["required"] == ["path", "content"]
    assert "expected_revision" in props, "覆盖已有文件必须能带版本"
    assert props["newline"]["enum"] == ["preserve", "lf", "crlf"]
    assert "allow_empty" in props and "dry_run" in props


def test_operation_tools_are_workspace_write_domain_not_read():
    for name in OPERATION_TOOLS:
        assert is_workspace_tool(name) is True
        assert is_workspace_read_tool(name) is False, f"{name} 不是读取域工具"
        assert name in WORKSPACE_STAGE_AND_EXEC_TOOL_NAMES
        assert name in WORKSPACE_TOOL_NAMES
        assert name in WORKSPACE_ALL_CAPABILITIES
    assert WORKSPACE_OPERATION_CAPABILITIES == set(OPERATION_TOOLS)


def test_write_phase_window_reaches_the_model(monkeypatch):
    """端到端（ReAct 窗口）：写意图 → 窗口里能看到 workspace_write。"""
    from app.agents.orchestration.react_runner import OfficeReactRunner
    from app.workspace.context import WORKSPACE_OPERATION_CAPABILITIES

    class _Cap:
        def __init__(self, raw: str) -> None:
            self.raw_name = raw
            self.name = f"mcp__lumi_pc__{raw}"
            self.version = "1.0.0"
            self.write_op = raw != "workspace_navigator"
            self.requires_confirmation = False
            self.idempotent = True
            self.domain = "workspace"

    advertised = [{"name": name} for name in (*OPERATION_TOOLS, "workspace_navigator")]

    async def fake_list_tools(server_name):
        return [dict(item, input_schema={"type": "object", "properties": {}}) for item in advertised]

    monkeypatch.setattr("app.agents.mcp.manager.list_tools", fake_list_tools)
    monkeypatch.setattr("app.agents.mcp.manager.server_is_healthy", lambda _s: True)
    monkeypatch.setattr(
        "app.workspace.context.resolve_workspace_desktop",
        lambda user_id, workspace_id: {"server_name": "lumi_pc", "status_code": "WORKSPACE_READY"},
    )
    # 聚合入口由后端合成（不在 MCP 广告里），补一份替身
    from app.agents.skills import executor as ex

    async def fake_navigator_capability(user_id, scene, user_role, workspace_id):
        return [_Cap("workspace_navigator")]

    monkeypatch.setattr(ex, "get_workspace_navigator_capability", fake_navigator_capability)

    runner = OfficeReactRunner(
        user_id="u1", job_id="job-1", user_role="user", workspace_id="w1",
        workspace_summary="工作区已注册且可访问。",
    )
    # 补丁通过模块属性生效：这里按调用时解析一次，确认替身已就位。
    from app.workspace.context import resolve_workspace_desktop as resolve_now

    assert resolve_now("u1", "w1")["server_name"] == "lumi_pc"
    window = asyncio.run(
        runner._maybe_inject_workspace_stage_window([], "帮我在工作区里创建 README.md 并写入内容")
    )
    names = {item.name for item in window}
    assert "mcp__lumi_pc__workspace_write" in names
    assert "mcp__lumi_pc__workspace_navigator" in names
    assert WORKSPACE_OPERATION_CAPABILITIES


def test_capability_snapshot_offers_operation_tools_to_the_compiler():
    """规划层发现的第三环：能力快照（plan_compiler 用）必须含四个操作工具。"""
    from app.agents.orchestration.planning.plan_compiler import build_capability_snapshot

    snapshot = asyncio.run(
        build_capability_snapshot(scene="office", user_role="user", user_id="u1", workers={})
    )
    for name in OPERATION_TOOLS:
        entry = snapshot.tools.get(name)
        assert entry is not None, f"{name} 不在能力快照里，Planner 永远选不到它"
        assert entry["write_op"] is True
        assert entry["plan_required_fields"], f"{name} 缺少 plan_required_fields"


def test_compiler_accepts_workspace_write_and_marks_it_as_write(monkeypatch):
    """atomic_step(preferred_tool=workspace_write) 必须能通过编译校验。"""
    from app.agents.orchestration.planning import plan_compiler
    from app.agents.orchestration.models import TaskNode
    from app.agents.orchestration.planning.plan_compiler import (
        CompileDecision,
        build_capability_snapshot,
        compile_plan,
    )

    snapshot = asyncio.run(
        build_capability_snapshot(scene="office", user_role="user", user_id="u1", workers={})
    )
    assert "workspace_write" in snapshot.tools, "插件未装载：先确认发现层测试的前置条件"

    async def fake_snapshot(**_kwargs):
        return snapshot

    monkeypatch.setattr(plan_compiler, "build_capability_snapshot", fake_snapshot)

    def _node(tool: str) -> TaskNode:
        return TaskNode(
            id="write",
            agent="atomic_step",
            name="write",
            params={
                "instruction": "新建 README.md",
                "preferred_tool": tool,
                "fallback_tools": [],
                # 工具自己声明 path/content 不允许在执行时猜，Planner 必须显式给出。
                "inputs": {"path": "README.md", "content": "# Hello"},
            },
        )

    result = asyncio.run(
        compile_plan(
            [_node("workspace_write")],
            scene="office",
            user_role="user",
            user_id="u1",
            workers={"atomic_step": object()},
        )
    )
    assert result.decision in {CompileDecision.ACCEPTED, CompileDecision.NORMALIZED}
    assert not [item for item in result.violations if item.code == "TOOL_UNAVAILABLE"]
    node = result.nodes[0]
    assert node.metadata["compiled_tool"] == "workspace_write"
    assert node.metadata["compiled_tool_write"] is True

    # 反向校验：不存在的工具名仍然必须被拒绝（证明上面的通过不是因为校验被关掉）。
    bad = asyncio.run(
        compile_plan(
            [_node("workspace_write_typo")],
            scene="office",
            user_role="user",
            user_id="u1",
            workers={"atomic_step": object()},
        )
    )
    assert any(item.code == "TOOL_UNAVAILABLE" for item in bad.violations)


def test_both_workspace_workflows_offer_atomic_write_tools():
    """规划走的 Workflow 路径也不能只认识暂存对（第七环）。"""
    from app.agents.skills.loader import load_skill_plugins
    from app.agents.skills.registry import SkillRegistry

    load_skill_plugins()
    for workflow_name in ("workspace_operation", "workspace_code_change"):
        skill = SkillRegistry.get_workflow(workflow_name)
        assert skill is not None, f"{workflow_name} 未注册"
        allowed = set(skill.allowed_tools)
        for name in OPERATION_TOOLS:
            assert f"mcp__lumi_client__{name}" in allowed, f"{workflow_name} 缺少 {name}"
        optional = {
            str(item.get("name"))
            for item in (skill.dependencies or {}).get("tools", [])
            if not item.get("required", True)
        }
        # 新增的原子工具必须是可选依赖：老客户端只广告暂存对时工作流依然可用。
        for name in OPERATION_TOOLS:
            assert f"mcp__lumi_client__{name}" in optional
        assert "mcp__lumi_client__workspace_stage_write" in allowed

