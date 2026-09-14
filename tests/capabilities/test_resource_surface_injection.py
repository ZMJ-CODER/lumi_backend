"""Phase 5 第二步：注入路径的收敛（模型可见名 + 执行期解析）回归。

上半步（`test_resource_surface.py`）证明"可见面可计算、差异可对拍"；这一步证明
**收敛真的能跑**：

```text
模型看到 Write  →  StructuredTool(name="Write")
                →  execute_tool_call(function.name="Write")
                →  _resolve_model_alias  →  workspace_write（实现名，审计可见）
                →  租约/审批/版本校验/写闸（与改造前完全同一条路径）
```

三条不变量：

1. **默认关闭逐字不变**：`collapse_for_surface(enabled=False)` 是恒等映射，且不去重；
2. **收敛只减少名字、不减少能力**：同一对外名保留**第一个**（候选池已按相关性排序）；
3. **未登记/不收敛的工具不能被合并或被改名**（"不认识"不等于"可以拿走"）。
"""

from __future__ import annotations

import json

import pytest

from app.agents.capabilities.views import resource_surface as sf


def _cap(name: str, **kwargs):
    from app.agents.skills.capability import ToolCapability

    return ToolCapability(name=name, **kwargs)


@pytest.fixture()
def surface_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", False)
    return settings


@pytest.fixture()
def surface_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", True)
    return settings


# ── 1. 收敛映射 ─────────────────────────────────────────────


def test_collapse_is_identity_when_disabled(surface_off):
    pool = [
        _cap("workspace_navigator"),
        _cap("workspace_read"),
        _cap("workspace_write"),
        _cap("AskUserQuestion"),
    ]
    pairs = sf.collapse_for_surface(pool)
    assert [(item.name, display) for item, display in pairs] == [
        ("workspace_navigator", "workspace_navigator"),
        ("workspace_read", "workspace_read"),
        ("workspace_write", "workspace_write"),
        ("AskUserQuestion", "AskUserQuestion"),
    ], "关闭时必须逐字等于改造前（含不去重）"


def test_collapse_renames_and_dedupes_when_enabled(surface_on):
    pool = [
        _cap("workspace_navigator"),
        _cap("workspace_read"),
        _cap("workspace_search"),
        _cap("workspace_write"),
        _cap("AskUserQuestion"),
        _cap("create_office_document"),
    ]
    pairs = sf.collapse_for_surface(pool)
    assert [(item.name, display) for item, display in pairs] == [
        ("workspace_navigator", "Read"),   # 别名合并，保留第一个（候选池已排序）
        ("workspace_search", "Search"),
        ("workspace_write", "Write"),
        ("AskUserQuestion", "AskUserQuestion"),          # 未登记 → 原名
        ("create_office_document", "create_office_document"),  # 刻意不收敛 → 原名
    ]


def test_collapse_keeps_the_first_capability_of_each_display_name(surface_on):
    """合并的是**名字**，不是能力：被合并掉的别名不再单独暴露。"""
    first = _cap("workspace_navigator")
    second = _cap("workspace_read")
    pairs = sf.collapse_for_surface([first, second])
    assert len(pairs) == 1
    assert pairs[0][0] is first


def test_display_name_for_unknown_and_unconverged():
    assert sf.display_name_for("workspace_move") in {sf.MODEL_MOVE, "workspace_move"}
    assert sf.display_name_for("AskUserQuestion") == "AskUserQuestion"
    assert sf.display_name_for("create_office_document") == "create_office_document"
    assert sf.display_name_for("") == ""


# ── 2. 注入：StructuredTool 对外名 + 执行传对外名 ────────────


@pytest.mark.asyncio
async def test_chat_tool_injection_uses_display_name(surface_on, monkeypatch):
    """收敛打开时：模型看到的名字是对外名，且执行器收到的是同一个名字。"""
    from app.agents.langchain import tools as tools_module
    from app.agents.skills.capability import ToolCapability

    async def _capability(name, *_args, **_kwargs):
        return ToolCapability(name=name, description="d", parameters={"type": "object", "properties": {}})

    monkeypatch.setattr(tools_module, "get_tool_capability", _capability)
    captured: list[str] = []

    async def _execute(tool_call, *_args, **_kwargs):
        captured.append(str((tool_call.get("function") or {}).get("name") or ""))
        from app.agents.skills.base import ToolOutput

        return ToolOutput(success=True, output="ok", data={})

    monkeypatch.setattr(tools_module, "execute_tool_call", _execute)
    tool = await tools_module.make_skill_tool(
        "workspace_write", user_id="u1", scene="office", display_name="Write"
    )
    assert tool is not None
    assert tool.name == "Write", "模型看到的是对外名"
    await tool.coroutine(path="a.txt")
    assert captured == ["Write"], "执行器收到的也是对外名（由它统一解析回实现名）"


@pytest.mark.asyncio
async def test_chat_tool_injection_without_display_name_is_unchanged(surface_off, monkeypatch):
    """关闭收敛（或没给对外名）时，工具名与执行参数都等于实现名。"""
    from app.agents.langchain import tools as tools_module
    from app.agents.skills.capability import ToolCapability

    async def _capability(name, *_args, **_kwargs):
        return ToolCapability(name=name, description="d", parameters={"type": "object", "properties": {}})

    monkeypatch.setattr(tools_module, "get_tool_capability", _capability)
    captured: list[str] = []

    async def _execute(tool_call, *_args, **_kwargs):
        captured.append(str((tool_call.get("function") or {}).get("name") or ""))
        from app.agents.skills.base import ToolOutput

        return ToolOutput(success=True, output="ok", data={})

    monkeypatch.setattr(tools_module, "execute_tool_call", _execute)
    tool = await tools_module.make_skill_tool("workspace_write", user_id="u1", scene="office")
    assert tool is not None and tool.name == "workspace_write"
    await tool.coroutine(path="a.txt")
    assert captured == ["workspace_write"]


@pytest.mark.asyncio
async def test_display_name_resolves_back_to_implementation(surface_on, monkeypatch):
    """端到端（执行器侧）：对外名能被解析回实现名，并且**不是**靠猜。"""
    import app.agents.skills.executor as executor

    monkeypatch.setattr(
        executor, "_registered_tool_names", lambda: ("workspace_write", "workspace_navigator")
    )
    resolved, meta = executor._resolve_model_alias("Write")
    assert resolved == "workspace_write"
    assert meta["model_tool"] == "Write"
    assert meta["capability"] == "resource.write"
    # 参数里的 JSON 与名字无关：确认工具参数不受改名影响
    assert json.loads(json.dumps({"path": "a.txt"})) == {"path": "a.txt"}


@pytest.mark.asyncio
async def test_unresolvable_display_name_fails_loudly(surface_on, monkeypatch):
    """对外名解析不出实现时，必须报结构化失败——不能静默挑一个别的工具。"""
    import app.agents.skills.executor as executor

    monkeypatch.setattr(executor, "_registered_tool_names", lambda: ())
    result = await executor.execute_tool_call(
        {"function": {"name": "Write", "arguments": "{}"}},
        user_id="u1",
        scene="office",
    )
    assert result.success is False
    assert result.error_code == "MODEL_ALIAS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_office_and_unbound_tools_are_not_renamed(surface_on, monkeypatch):
    """办公文档与本机动作不参与收敛：调用路径必须逐字不变。"""
    import app.agents.skills.executor as executor

    monkeypatch.setattr(executor, "_registered_tool_names", lambda: ("office_doc_edit",))
    assert executor._resolve_model_alias("office_doc_edit") == ("office_doc_edit", {})
    assert executor._resolve_model_alias("AskUserQuestion") == ("AskUserQuestion", {})


# ── 4. ReAct：名字型护栏必须走实现名 ────────────────────────


def test_react_impl_name_mapping_is_identity_by_default():
    from app.agents.orchestration.react_runner import OfficeReactRunner

    runner = OfficeReactRunner(user_id="u1", job_id="j1")
    assert runner._surface_alias == {}
    assert runner._impl_name("workspace_write") == "workspace_write"
    assert runner._impl_name("Write") == "Write", "没有映射表时恒等（关闭收敛）"


def test_react_impl_name_resolves_converged_names():
    from app.agents.orchestration.react_runner import OfficeReactRunner

    runner = OfficeReactRunner(user_id="u1", job_id="j1")
    runner._surface_alias = {"Write": "workspace_write", "Edit": "workspace_edit", "Move": "Rename"}
    assert runner._impl_name("Write") == "workspace_write"
    assert runner._impl_name("search_tools") == "search_tools", "编排原语不受影响"
    # 护栏按实现名判断：`Move` 是**新增**的对外名，护栏词表里只有 `rename`
    assert OfficeReactRunner._requires_prior_read(runner._impl_name("Move")) is True
    assert OfficeReactRunner._requires_prior_read("Move") is False, "对外名不在护栏词表里"
    assert OfficeReactRunner._is_read_tool(runner._impl_name("Read")) is True
