"""Phase 5 第三步：ReAct 工具面收敛（模型可见名 + 名字型护栏走实现名）。

ReAct 与 chat 路径的关键差别：它有**基于名字的安全逻辑**——

* ``_requires_prior_read(name)``：改/删之前必须读过同一目标；
* ``_successful_reads``：读过哪些目标；
* ``_failed_tools``：候选池按**实现名**排除失败方法；
* ``_call_key``：相同工具+参数的重复调用去重。

因此收敛在 ReAct 里不能只改 schema：所有名字型判断必须先解析回**实现名**，
否则模型叫一声 ``Write`` 就能绕过针对 ``workspace_edit`` 的"改前必须先读"护栏。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from app.agents.orchestration.react_runner import OfficeReactRunner
from app.agents.skills.base import SkillResult, Tool
from app.agents.skills.capability import ToolCapability
from app.agents.skills.registry import ToolRegistry


class _Bound:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, messages):
        return await self.model.ainvoke(messages)


class _Model:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.tools = []

    def bind_tools(self, tools):
        self.tools = list(tools)
        return _Bound(self)

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return self.replies.pop(0)


class _FakeWorkspaceTool(Tool):
    """最小工作区工具替身：只为验证"模型名 → 实现名"的接线。"""

    scenes = ["office"]
    environment = "client"
    parameters_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "expected_revision": {"type": "integer"}},
    }

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"fake {name}"

    async def execute(self, params, context=None):
        return SkillResult(success=True, output=f"{self.name}:{params.get('path')}")


def _install_tools(*names: str) -> None:
    for name in names:
        ToolRegistry.register(_FakeWorkspaceTool(name))


def _capabilities(*names: str):
    async def _load(*_args, **_kwargs):
        return [ToolCapability(name=name, description=f"fake {name}", category="devtools") for name in names]

    return _load


def _run(monkeypatch, replies, *, converged: bool):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_SURFACE", converged)
    # 裸注册的替身工具不在 base_tools.yaml 白名单里；这里关掉白名单闸，专测"工具面收敛"。
    # （白名单本身是方案 §六的独立议题，不属于本阶段。）
    monkeypatch.setattr(settings, "AGENT_BASE_TOOLS_ONLY", False)
    monkeypatch.setattr(settings, "AGENT_TOOL_WRITE_ENABLED", True)
    model = _Model(replies)
    monkeypatch.setattr(
        "app.agents.orchestration.react.runner.get_chat_model",
        lambda **_kwargs: asyncio.sleep(0, result=model),
    )
    monkeypatch.setattr(
        "app.agents.orchestration.react.agent_node.get_office_react_capabilities_with_trace",
        _capabilities("workspace_navigator", "workspace_edit", "workspace_write"),
    )
    # ``domain_first=False``：本轮验证的是"给定候选池时的工具面"，不是领域发现流程
    # （domain_first 模式下第一轮只给两个发现原语，那是既有行为）。
    result = asyncio.run(
        OfficeReactRunner(user_id="u1", job_id="j1", domain_first=False).run("修改工作区文件")
    )
    return model, result


def _tool_message_names(model):
    names: list[str] = []
    for call in model.calls:
        for message in call:
            if isinstance(message, ToolMessage):
                names.append(str(message.name or ""))
    return names


def test_react_bound_tools_use_display_names_when_converged(monkeypatch):
    _install_tools("workspace_navigator", "workspace_edit", "workspace_write")
    try:
        model, _result = _run(
            monkeypatch,
            [AIMessage(content="已了解工作区")],
            converged=True,
        )
        bound = {getattr(tool, "name", "") for tool in model.tools}
        assert {"Read", "Edit", "Write"} <= bound, bound
        assert not ({"workspace_navigator", "workspace_edit", "workspace_write"} & bound), bound
    finally:
        for name in ("workspace_navigator", "workspace_edit", "workspace_write"):
            ToolRegistry.unregister(name)


def test_react_bound_tools_keep_implementation_names_when_disabled(monkeypatch):
    _install_tools("workspace_navigator", "workspace_edit", "workspace_write")
    try:
        model, _result = _run(monkeypatch, [AIMessage(content="ok")], converged=False)
        bound = {getattr(tool, "name", "") for tool in model.tools}
        assert {"workspace_navigator", "workspace_edit", "workspace_write"} <= bound, bound
    finally:
        for name in ("workspace_navigator", "workspace_edit", "workspace_write"):
            ToolRegistry.unregister(name)


def test_react_guard_still_blocks_write_before_read_when_converged(monkeypatch):
    """**核心安全断言**：模型叫 ``Edit`` 也必须先读（``Edit`` → ``workspace_edit``）。

    收敛如果只改 schema 而没把护栏切到实现名，这条会失败：``Edit`` 不在
    ``_requires_prior_read`` 的实现名词表里（那里是 ``workspace_edit``）。

    ``Write`` 刻意不在护栏里——它可以合法地新建文件；``workspace_edit`` 必须前置读取，
    因为它的契约要求 ``expected_revision``，而 revision 只能从读取拿到。
    """
    _install_tools("workspace_navigator", "workspace_edit", "workspace_write")
    try:
        model, result = _run(
            monkeypatch,
            [
                # ① 直接改：必须被护栏拦下
                AIMessage(content="", tool_calls=[
                    {"name": "Edit", "args": {"path": "a.txt", "expected_revision": 1}, "id": "c1"}
                ]),
                # ② 先读：通过
                AIMessage(content="", tool_calls=[
                    {"name": "Read", "args": {"path": "a.txt"}, "id": "c2"}
                ]),
                # ③ 再改：这次应真的走到工具
                AIMessage(content="", tool_calls=[
                    {"name": "Edit", "args": {"path": "a.txt", "expected_revision": 1}, "id": "c3"}
                ]),
                AIMessage(content="完成"),
            ],
            converged=True,
        )
        guard_hits = [
            message
            for call in model.calls
            for message in call
            if isinstance(message, ToolMessage) and "安全护栏" in str(message.content)
        ]
        assert guard_hits, "改之前的读取护栏必须仍然生效"
        assert str(guard_hits[0].name) == "Edit", "护栏错误要回给模型看到的那个名字"
        later = [
            message
            for call in model.calls
            for message in call
            if isinstance(message, ToolMessage) and str(message.name) == "Edit"
        ]
        assert len(later) >= 2, "第③步应该真的尝试执行"
        assert "安全护栏" not in str(later[-1].content), "读过之后不再报护栏错误"
        assert result.success is True
    finally:
        for name in ("workspace_navigator", "workspace_edit", "workspace_write"):
            ToolRegistry.unregister(name)
