"""统一资源能力层 Phase 5（模型可见面收敛）回归。

收敛是整个改造里**唯一会拿走东西**的一步：模型可见工具从 60+ 个实现名收敛到
`Read / Write / Edit / Move / Delete / Run / Search`。因此这里守三件事：

1. **可计算**：给定一批工具，算出收敛后的名字与"哪些名字会消失"；
2. **默认关闭、逐字不变**：注入路径不受影响（开关关闭时本模块只被读来做影子展示）；
3. **分类不了就不隐藏**：没有统一能力绑定的工具（本机动作、编排原语、外部服务、
   刻意不收敛的 `artifact.create`）原样保留——"不认识"绝不等于"可以拿走"。
"""

from __future__ import annotations

import json

import pytest

from app.agents.capabilities.views import resource_surface as sf


def _cap(name: str):
    from app.agents.skills.capability import ToolCapability

    return ToolCapability(name=name)


@pytest.fixture()
def surface_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    return settings


@pytest.fixture()
def surface_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", False)
    return settings


def test_flag_defaults_to_off():
    from app.core.config import settings

    assert settings.RESOURCE_CAPABILITY_SURFACE is False
    assert sf.surface_enabled() is False


# ── 1. 名称映射 ─────────────────────────────────────────────


def test_model_tool_for_capability():
    assert sf.model_tool_for("resource.read") == sf.MODEL_READ
    assert sf.model_tool_for("resource.write") == sf.MODEL_WRITE
    assert sf.model_tool_for("resource.edit") == sf.MODEL_EDIT
    assert sf.model_tool_for("resource.move") == sf.MODEL_MOVE
    assert sf.model_tool_for("resource.delete") == sf.MODEL_DELETE
    assert sf.model_tool_for("code.execute") == sf.MODEL_RUN
    # 检索语义下读取能力的对外名字是 Search（两条判定路径）
    assert sf.model_tool_for("resource.read", intent="SEARCH") == sf.MODEL_SEARCH
    assert sf.model_tool_for("resource.read", tool="Glob") == sf.MODEL_SEARCH
    assert sf.model_tool_for("resource.read", tool="mcp__lumi_client__workspace_search") == sf.MODEL_SEARCH
    # 聚合入口主要承担 read（检索用 action=search 表达）→ 仍是 Read
    assert sf.model_tool_for("resource.read", tool="workspace_navigator") == sf.MODEL_READ
    assert sf.model_tool_for("resource.write", intent="SEARCH") == sf.MODEL_WRITE
    # 别名归一
    assert sf.model_tool_for("sandbox.run") == sf.MODEL_RUN


def test_unconverged_and_unknown_capabilities_have_no_model_tool():
    """**刻意不收敛**的能力（artifact.create）与未知能力都不给模型名。"""
    assert sf.model_tool_for("artifact.create") == ""
    assert sf.model_tool_for("nonsense") == ""
    assert sf.model_tool_for("") == ""


def test_model_facing_vocabulary_matches_the_plan():
    assert sf.MODEL_FACING_TOOLS == ("Read", "Write", "Edit", "Move", "Delete", "Run", "Search")
    assert set(sf.MODEL_ALIASES) == set(sf.MODEL_FACING_TOOLS)
    from app.agents.capabilities.catalog.resource import UNIFIED_CAPABILITIES

    for name, (capability, preferred) in sf.MODEL_ALIASES.items():
        assert capability in UNIFIED_CAPABILITIES, name
        assert preferred, name


# ── 2. 可见面计算 ───────────────────────────────────────────


def test_converged_face_collapses_workspace_aliases():
    pool = [
        _cap("workspace_navigator"),
        _cap("workspace_read"),
        _cap("workspace_search"),
        _cap("workspace_write"),
        _cap("mcp__lumi_client__workspace_edit"),
    ]
    face = sf.converged_face(pool)
    # 读取族收敛成一个 Read；专用检索入口另给一个 Search（两者都是读取能力）
    assert face == (sf.MODEL_READ, sf.MODEL_SEARCH, sf.MODEL_WRITE, sf.MODEL_EDIT), face
    # 没有专用检索入口时不再出现 Search
    assert sf.converged_face([_cap("workspace_navigator"), _cap("workspace_read")]) == (sf.MODEL_READ,)
    # 顺序稳定：按输入顺序取第一个出现的名字
    assert sf.converged_face([_cap("workspace_write"), _cap("workspace_read")]) == (
        sf.MODEL_WRITE,
        sf.MODEL_READ,
    )


def test_unclassified_tools_are_never_hidden():
    """安全不变量：分类不了的工具原样保留在当前可见面里。"""
    pool = [
        _cap("workspace_write"),
        _cap("AskUserQuestion"),
        _cap("desktop_open_url"),
        _cap("create_office_document"),  # 刻意不收敛
        _cap("totally_unknown_tool"),
    ]
    face = sf.converged_face(pool)
    assert sf.MODEL_WRITE in face
    for kept in ("AskUserQuestion", "desktop_open_url", "create_office_document", "totally_unknown_tool"):
        assert kept in face, kept
    hidden = sf.hidden_tools(pool)
    assert hidden == ["workspace_write"]
    # 会消失的工具**必须**有统一能力绑定（否则就是误伤）
    for entry in sf.surface_entries(pool):
        if entry.action == "hidden":
            assert entry.capability and entry.resource_type, entry


def test_surface_diff_rows_are_grouped_and_three_wide():
    pool = [
        _cap("workspace_write"),
        _cap("workspace_stage_write"),
        _cap("workspace_edit"),
        _cap("AskUserQuestion"),
    ]
    rows = sf.surface_diff(pool)
    # ``workspace_stage_write`` 是"七个动词表达不了的阶段动作" → 保留原名、不出现在差异里
    assert rows == [
        ["resource.edit", "workspace_edit", sf.MODEL_EDIT],
        ["resource.write", "workspace_write", sf.MODEL_WRITE],
    ]
    assert all(len(row) == 3 for row in rows), "行形状必须与其它影子维度一致（三格）"


def test_surface_diff_is_empty_when_nothing_changes():
    assert sf.surface_diff([_cap("AskUserQuestion"), _cap("create_office_document")]) == []
    assert sf.surface_diff([]) == []


def test_surface_entries_report_action_and_binding():
    entries = {
        item.tool: item
        for item in sf.surface_entries(
            [_cap("workspace_write"), _cap("Bash"), _cap("Read"), _cap("Write")]
        )
    }
    write = entries["workspace_write"]
    assert (write.capability, write.resource_type, write.model_tool, write.action) == (
        "resource.write", "workspace", sf.MODEL_WRITE, "hidden",
    )
    # 客户端历史名与对外名不同的会"改名"（Bash → Run），因此也算 hidden
    bash = entries["Bash"]
    assert (bash.model_tool, bash.action) == (sf.MODEL_RUN, "hidden")
    # Read / Write 与客户端原子名同名 → 原地保留（无需改名）
    assert entries["Read"].action == "kept"
    assert entries["Write"].action == "kept"
    json.dumps([item.as_dict() for item in entries.values()], ensure_ascii=False)


def test_only_rename_needing_tools_disappear():
    """哪些名字要"改名"是设计的一部分：Read/Write/Edit/Delete 同名，Move/Run/Search 需要别名。

    这条断言把"改名集合"钉住：以后谁想改对外名字，必须先想清楚
    ``resolve_alias`` 有没有对应实现（否则模型叫了名字却落不到工具上）。
    """
    pool = [_cap(name) for name in ("Read", "Write", "Edit", "Delete", "Rename", "Bash", "Glob", "Grep")]
    kept = {entry.tool for entry in sf.surface_entries(pool) if entry.action == "kept"}
    hidden = {entry.tool for entry in sf.surface_entries(pool) if entry.action == "hidden"}
    assert kept == {"Read", "Write", "Edit", "Delete"}
    assert hidden == {"Rename", "Bash", "Glob", "Grep"}
    assert sf.converged_face(pool) == (
        sf.MODEL_READ, sf.MODEL_WRITE, sf.MODEL_EDIT, sf.MODEL_DELETE,
        sf.MODEL_MOVE, sf.MODEL_RUN, sf.MODEL_SEARCH,
    )


# ── 3. 别名解析（执行期用）─────────────────────────────────


def test_resolve_alias_prefers_client_atomic_names():
    available = ["Rename", "workspace_move", "Bash", "sandbox_run", "Glob"]
    assert sf.resolve_alias("Move", available) == ("Rename", "resource.move")
    assert sf.resolve_alias("Run", available) == ("Bash", "code.execute")
    assert sf.resolve_alias("Search", available) == ("Glob", "resource.read")
    assert sf.resolve_alias("Read", ["workspace_navigator"]) == ("workspace_navigator", "resource.read")


def test_resolve_alias_falls_back_to_provider_adapter():
    """客户端没广告历史名时，用 Provider Adapter 的规范工具（新 Provider 走这条）。"""
    tool, capability = sf.resolve_alias("Write", ["workspace_write"])
    assert (tool, capability) == ("workspace_write", "resource.write")


def test_resolve_alias_returns_empty_when_nothing_available():
    """解析不出来就返回空：调用方必须报结构化失败，不能静默换一个工具。"""
    assert sf.resolve_alias("Move", []) == ("", "resource.move")
    assert sf.resolve_alias("Move", ["totally_unknown"]) == ("", "resource.move")
    # 不是模型可见名的输入 → 既没有工具也没有能力
    assert sf.resolve_alias("workspace_write", ["workspace_write"]) == ("", "")


def test_is_model_facing():
    for name in sf.MODEL_FACING_TOOLS:
        assert sf.is_model_facing(name) is True
    assert sf.is_model_facing("workspace_write") is False
    assert sf.is_model_facing("") is False


# ── 4. 快照与真实注册表 ─────────────────────────────────────


def test_surface_snapshot_is_json_safe():
    snapshot = sf.surface_snapshot()
    json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["model_facing_tools"] == list(sf.MODEL_FACING_TOOLS)
    assert snapshot["unconverged_capabilities"] == ["artifact.create"]
    assert snapshot["aliases"]["Move"]["preferred_tools"][0] == "Rename"


def test_real_registry_surface_is_measurable():
    """真实注册表上能算出收敛面与"会消失的名字"（Phase 5 的切换前提）。"""
    from app.agents.capabilities.catalog.tool_registry import entries_by_name

    entries = entries_by_name()
    hidden = sf.hidden_tools(entries.values())
    assert "workspace_write" in hidden
    # 七个动词表达不了的阶段动作保留原名（收敛边界，见 UNCONVERGED_TOOLS）
    assert "workspace_stage_write" not in hidden
    assert "workspace_commit" not in hidden
    assert sf.display_name_for("workspace_stage_write") == "workspace_stage_write"
    assert "create_office_document" not in hidden, "刻意不收敛的产物工具不能被隐藏"
    face = sf.converged_face(entries.values())
    for name in (sf.MODEL_READ, sf.MODEL_WRITE, sf.MODEL_EDIT, sf.MODEL_DELETE, sf.MODEL_RUN):
        assert name in face, name
    # 收敛面**一定比当前面小**（否则这次收敛没有意义）
    assert len(face) < len(entries)
    # 未登记的工具一律保留原名
    for entry in sf.surface_entries(entries.values()):
        if entry.action == "hidden":
            assert entry.capability, entry.tool


# ── 5. 执行期接线（模型可见名 → 真实工具）───────────────────


@pytest.mark.asyncio
async def test_alias_resolution_is_identity_when_disabled(surface_off):
    from app.agents.skills.executor import _resolve_model_alias

    for name in ("Move", "Run", "Search", "Read", "workspace_write"):
        assert _resolve_model_alias(name) == (name, {}), name


@pytest.mark.asyncio
async def test_alias_resolution_prefers_client_atomic_names(surface_on, monkeypatch):
    """池里有客户端历史名时优先用它（模型真正调得到的是客户端工具）。"""
    import app.agents.skills.executor as executor

    monkeypatch.setattr(
        executor,
        "_registered_tool_names",
        lambda: ("Rename", "Bash", "Glob", "workspace_move", "workspace_write"),
    )
    assert executor._resolve_model_alias("Move") == (
        "Rename", {"model_tool": "Move", "capability": "resource.move"},
    )
    assert executor._resolve_model_alias("Run")[0] == "Bash"
    assert executor._resolve_model_alias("Search")[0] == "Glob"
    # 与客户端原子名同名的（Read/Write）原地保留
    assert executor._resolve_model_alias("workspace_write") == ("workspace_write", {})


@pytest.mark.asyncio
async def test_alias_resolution_falls_back_to_server_side_names(surface_on, monkeypatch):
    """池里只有服务端名字时落到规范工具（Provider Adapter 的答案）。

    刻意把可用名固定下来：真实进程里"哪些名字已注册"取决于加载了哪些插件，
    断言具体名字会让测试随环境变化（实测在整包运行时 `Move` 会解析到客户端 `Rename`）。
    """
    import app.agents.skills.executor as executor

    monkeypatch.setattr(
        executor,
        "_registered_tool_names",
        lambda: ("workspace_move", "sandbox_run", "workspace_search", "workspace_navigator"),
    )
    assert executor._resolve_model_alias("Move")[0] == "workspace_move"
    assert executor._resolve_model_alias("Run")[0] == "sandbox_run"
    assert executor._resolve_model_alias("Search")[0] == "workspace_search"
    assert executor._resolve_model_alias("Read")[0] == "workspace_navigator"
    # 不是模型可见名 → 原样
    assert executor._resolve_model_alias("Nonsense") == ("Nonsense", {})


@pytest.mark.asyncio
async def test_execute_tool_call_reports_structured_failure_for_unresolvable_alias(
    surface_on, monkeypatch
):
    """别名解析不出 → 稳定错误码 ``MODEL_ALIAS_UNAVAILABLE``（不静默换工具）。"""
    import app.agents.skills.executor as executor

    monkeypatch.setattr(executor, "_registered_tool_names", lambda: ())
    result = await executor.execute_tool_call(
        {"function": {"name": "Move", "arguments": "{}"}}, user_id="u1", scene="office"
    )
    assert result.success is False
    assert result.error_code == "MODEL_ALIAS_UNAVAILABLE"
    assert result.retryable is True
    assert result.metadata.get("model_tool") == "Move"


@pytest.mark.asyncio
async def test_execute_tool_call_alias_off_goes_legacy_route(surface_off):
    """关闭开关时 ``Move`` 不会被当成别名：照旧按"未知技能"处理（逐字不变）。"""
    import app.agents.skills.executor as executor

    result = await executor.execute_tool_call(
        {"function": {"name": "Move", "arguments": "{}"}}, user_id="u1", scene="office"
    )
    assert result.success is False
    assert result.error_code == "SKILL_NOT_FOUND"
