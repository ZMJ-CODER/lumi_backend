"""普通聊天 LangGraph ToolNode 的安全与收敛测试。"""

import asyncio

import pytest
from langchain_core.messages import AIMessage

from app.agents.langchain.chat_graph import LangGraphChatRunner
from app.agents.skills.base import Skill, SkillResult
from app.agents.skills.executor import get_tools_for_scene, run_skill_loop
from app.agents.skills.registry import SkillRegistry
from app.services.orchestrator import _needs_chat_tool_graph


@pytest.fixture(autouse=True)
def _skills():
    from app.agents.skills import loader

    SkillRegistry.clear()
    loader.unload_skill_plugins()
    loader.load_skill_plugins()
    yield
    loader.unload_skill_plugins()
    SkillRegistry.clear()


class _BoundModel:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, messages):
        return await self.model.ainvoke(messages)


class _ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def bind_tools(self, tools):
        self.tools = tools
        return _BoundModel(self)

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return self.replies.pop(0)


class _GraphEchoSkill(Skill):
    name = "get_datetime"
    description = "echo"
    scenes = ["chat"]
    parameters_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, params, context=None):
        return SkillResult(success=True, output=f"echo:{params['text']}")


class _OfficeGraphEchoSkill(Skill):
    name = "office_graph_echo"
    description = "office graph echo"
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, params, context=None):
        return SkillResult(success=True, output=f"office:{params['text']}")


def test_chat_graph_runs_one_tool_at_a_time_and_keeps_tool_data_untrusted(monkeypatch):
    SkillRegistry.register(_GraphEchoSkill())
    model = _ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_datetime", "args": {"text": "first"}, "id": "c1"},
                    {"name": "get_datetime", "args": {"text": "second"}, "id": "c2"},
                ],
            ),
            AIMessage(content="完成"),
        ]
    )
    runner = LangGraphChatRunner(user_id="u1", chat_model=model, max_rounds=2)
    answer, records, citations = asyncio.run(runner.run([{"role": "user", "content": "测试"}]))

    assert answer == "完成"
    assert citations == []
    assert records == [{"skill": "get_datetime", "success": True, "error_code": None, "error": None}]
    # 第二次模型调用能看到工具返回，但其中内容被明确标记为数据。
    assert "不可信数据" in str(model.calls[1][-1].content)


def test_chat_graph_failure_becomes_safe_tool_message_and_max_round_forces_final_answer():
    class FailingDatetimeSkill(_GraphEchoSkill):
        async def execute(self, params, context=None):
            return SkillResult(success=False, error="临时不可用", error_code="TIMEOUT", retryable=True)

    SkillRegistry.register(FailingDatetimeSkill())
    model = _ScriptedModel(
        [
            AIMessage(content="", tool_calls=[{"name": "get_datetime", "args": {"text": "x"}, "id": "c1"}]),
            AIMessage(content="已根据当前可用信息说明限制。"),
        ]
    )
    runner = LangGraphChatRunner(user_id="u1", chat_model=model, max_rounds=1)
    answer, records, _ = asyncio.run(runner.run([{"role": "user", "content": "测试"}]))

    assert answer == "已根据当前可用信息说明限制。"
    assert records[0]["success"] is False
    assert records[0]["error_code"] == "TIMEOUT"
    # 到上限时走未绑定工具的收尾模型，避免模型继续追加工具调用。
    assert len(model.calls) == 2


def test_chat_graph_returns_decision_signals_after_a_tool_call():
    class SignalledSkill(_GraphEchoSkill):
        async def execute(self, params, context=None):
            return SkillResult(
                success=True,
                output="候选文档 A",
                metadata={"decision_signals": {"result_count": 1, "confidence_hint": {"level": "high", "basis": ["test_result"]}}},
            )

    SkillRegistry.register(SignalledSkill())
    model = _ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "get_datetime", "args": {"text": "x"}, "id": "c1"}]),
        AIMessage(content="完成"),
    ])
    answer, _, _ = asyncio.run(LangGraphChatRunner(user_id="u1", chat_model=model).run([
        {"role": "user", "content": "测试"},
    ]))
    assert answer == "完成"
    assert "决策信号" in str(model.calls[1][-1].content)
    assert "结果数=1" in str(model.calls[1][-1].content)


def test_tool_graph_supports_office_scene_with_serial_progress_events():
    SkillRegistry.register(_OfficeGraphEchoSkill())
    model = _ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "office_graph_echo", "args": {"text": "x"}, "id": "office-1"}],
            ),
            AIMessage(content="办公步骤已完成"),
        ]
    )
    progress = []
    runner = LangGraphChatRunner(
        user_id="u1",
        scene="office",
        chat_model=model,
        max_rounds=2,
        on_progress=progress.append,
    )
    answer, records, _ = asyncio.run(runner.run([{"role": "user", "content": "执行办公步骤"}]))

    assert answer == "办公步骤已完成"
    assert records == [{"skill": "office_graph_echo", "success": True, "error_code": None, "error": None}]
    assert [event["status"] for event in progress if isinstance(event, dict)] == ["running", "completed"]


def test_run_skill_loop_routes_office_lumi_client_to_langgraph(monkeypatch):
    """office 的生产 LLMClient 不能再默认掉回手写循环。"""
    from app.core.llm import LLMClient
    import app.agents.langchain.chat_graph as graph_mod

    captured = {}

    async def fake_run(self, messages):
        captured["scene"] = self.scene
        captured["messages"] = messages
        return "图执行完成", [], []

    monkeypatch.setattr(graph_mod.LangGraphChatRunner, "run", fake_run)
    result = asyncio.run(
        run_skill_loop(
            LLMClient(),
            "u1",
            [{"role": "user", "content": "执行办公工具"}],
            scene="office",
        )
    )

    assert result == ("图执行完成", [], [])
    assert captured["scene"] == "office"


def test_chat_tool_graph_is_intent_gated_and_never_exposes_office_tools():
    assert _needs_chat_tool_graph("请联网搜索今天的天气并给我来源") is True
    assert _needs_chat_tool_graph("帮我搜索今天的天气") is True
    assert _needs_chat_tool_graph("现在几点") is True
    assert _needs_chat_tool_graph("帮我总结刚上传的文档") is False
    assert _needs_chat_tool_graph("你好") is False
    names = {item["function"]["name"] for item in asyncio.run(get_tools_for_scene("chat"))}
    assert names <= {"web_search", "query_knowledge", "get_datetime", "calculator", "open_app"}
    assert "python_exec" not in names


def test_stream_chat_forwards_tool_progress_as_sse_steps(monkeypatch):
    """流式聊天不能在工具实际执行后把步骤信息丢失。"""
    import app.services.orchestrator as orchestrator_module

    orch = orchestrator_module.Orchestrator.__new__(orchestrator_module.Orchestrator)
    orch._llm = object()
    monkeypatch.setattr(orchestrator_module.settings, "AGENT_SKILLS_ENABLED", True)

    async def no_override(*args, **kwargs):
        return None

    async def fake_skill_loop(*args, **kwargs):
        progress = kwargs["on_progress"]
        progress({"type": "step", "id": "calculator-1", "title": "calculator", "status": "running", "tool": "calculator"})
        await asyncio.sleep(0)
        progress({"type": "step", "id": "calculator-1", "title": "calculator", "status": "completed", "tool": "calculator", "output": "42"})
        return "结果是 42", [{"skill": "calculator", "success": True}], []

    monkeypatch.setattr(orchestrator_module, "_get_chat_model_override", no_override)
    monkeypatch.setattr(orchestrator_module, "run_skill_loop", fake_skill_loop)

    async def scenario():
        return [
            event
            async for event in orch._stream_llm_auto(
                "u1",
                [{"role": "user", "content": "请用计算器计算"}],
                "chat",
                [],
                "请用计算器计算",
                [],
            )
        ]

    events = asyncio.run(scenario())
    steps = [event["step"] for event in events if event["type"] == "step"]
    assert [step["status"] for step in steps] == ["running", "completed"]
    assert events[-1] == {"type": "delta", "content": "结果是 42"}
