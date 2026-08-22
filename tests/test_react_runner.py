import asyncio

from langchain_core.messages import AIMessage

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.react_runner import OfficeReactRunner
from app.agents.roles.react import ReactStepAgent
from app.agents.core.base import WorkerContext
from app.agents.skills.base import Skill, SkillResult
from app.agents.skills.capability import ToolCapability
from app.agents.skills.registry import SkillRegistry


class _Bound:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, messages):
        return await self.model.ainvoke(messages)


class _Model:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def bind_tools(self, tools):
        self.tools = tools
        return _Bound(self)

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return self.replies.pop(0)


class _Echo(Skill):
    name = "react_echo"
    description = "react test echo"
    scenes = ["office"]
    parameters_schema = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, params, context=None):
        return SkillResult(success=True, output=f"echo:{params.get('text')}")


def test_office_react_runs_one_tool_per_round(monkeypatch):
    SkillRegistry.register(_Echo())
    model = _Model([
        AIMessage(content="", tool_calls=[
            {"name": "react_echo", "args": {"text": "one"}, "id": "r1"},
            {"name": "react_echo", "args": {"text": "two"}, "id": "r2"},
        ]),
        AIMessage(content="动态任务完成"),
    ])
    monkeypatch.setattr(
        "app.agents.orchestration.react_runner.get_chat_model",
        lambda **kwargs: asyncio.sleep(0, result=model),
    )
    async def test_capabilities(*args, **kwargs):
        return [ToolCapability(name="react_echo", description="react test echo", category="office")]

    monkeypatch.setattr(
        "app.agents.orchestration.react_runner.get_office_react_capabilities_for_request",
        test_capabilities,
    )
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("完成动态任务"))
    assert result.success is True
    assert result.content == "动态任务完成"
    assert [r["skill"] for r in result.records] == ["react_echo"]


def test_office_react_preserves_reasoning_payload_after_tool_call(monkeypatch):
    """Thinking-mode providers require reasoning_content on the tool-result turn."""
    SkillRegistry.register(_Echo())
    first = AIMessage(
        content="",
        tool_calls=[{"name": "react_echo", "args": {"text": "one"}, "id": "r1"}],
        additional_kwargs={"reasoning_content": "tool selection reasoning"},
    )
    model = _Model([first, AIMessage(content="完成")])
    monkeypatch.setattr(
        "app.agents.orchestration.react_runner.get_chat_model",
        lambda **kwargs: asyncio.sleep(0, result=model),
    )

    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(name="react_echo", description="react test echo", category="office")]

    monkeypatch.setattr(
        "app.agents.orchestration.react_runner.get_office_react_capabilities_for_request",
        capabilities,
    )
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("完成动态任务"))
    assert result.success is True
    replayed = next(message for message in model.calls[1] if isinstance(message, AIMessage))
    assert replayed.additional_kwargs["reasoning_content"] == "tool selection reasoning"


def test_m3_planner_creates_react_step():
    from app.agents.orchestration.planner import LlmPlanner
    from app.agents.orchestration.tca import ComplexityLevel

    tree = asyncio.run(LlmPlanner().plan_for_level(
        ComplexityLevel.M3, "u1", "分析销售下滑原因并给出建议", "office",
    ))
    assert len(tree.nodes) == 1
    assert tree.nodes[0].agent == "react_step"
    assert tree.nodes[0].params["max_rounds"] == 6


def test_react_worker_requires_instruction():
    result = asyncio.run(ReactStepAgent().execute(
        TaskNode(id="r1", agent="react_step"), WorkerContext(user_id="u1", job_id="j1")
    ))
    assert result["error_code"] == "INVALID_ARGS"


def test_react_worker_injects_manifest_predecessor_results(monkeypatch):
    received = {}

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        async def run(self, instruction, office_docs=None):
            received["instruction"] = instruction
            received["office_docs"] = office_docs
            from app.agents.orchestration.react_runner import ReactRunResult
            return ReactRunResult(True, content="完成")

    monkeypatch.setattr("app.agents.roles.react.OfficeReactRunner", FakeRunner)
    result = asyncio.run(ReactStepAgent().execute(
        TaskNode(
            id="r1",
            agent="react_step",
            params={
                "instruction": "检查第1项结果",
                "manifest_context": {"item-1": {"instruction": "生成摘要", "result": "摘要内容"}},
                "office_docs": [{"doc_id": "d1", "filename": "tasks.txt"}],
            },
        ),
        WorkerContext(user_id="u1", job_id="j1"),
    ))
    assert result["success"] is True
    assert "摘要内容" in received["instruction"]
    assert received["office_docs"] == [{"doc_id": "d1", "filename": "tasks.txt"}]


def test_office_react_recomputes_tools_and_excludes_failed_method(monkeypatch):
    model = _Model([
        AIMessage(content="", tool_calls=[{"name": "first", "args": {}, "id": "r1"}]),
        AIMessage(content="", tool_calls=[{"name": "second", "args": {}, "id": "r2"}]),
        AIMessage(content="已使用备用方法完成"),
    ])
    toolsets = []

    async def route(request, user_role, limit=8, excluded_names=None, user_id=""):
        excluded = set(excluded_names or [])
        names = ["first", "second"] if not excluded else ["second"]
        toolsets.append((request, excluded, names))
        return [ToolCapability(name=name, description=name, category="office", domain="document") for name in names]

    async def fake_tool(name, **kwargs):
        class Tool:
            async def ainvoke(self, args):
                result = SkillResult(
                    success=name == "second",
                    output="备用成功" if name == "second" else "",
                    error="第一种方法失败" if name == "first" else None,
                    error_code="EXEC_ERROR" if name == "first" else None,
                )
                await kwargs["on_result"](result)
                return result.output if result.success else f"工具未完成：{result.error}"
        return Tool()

    monkeypatch.setattr("app.agents.orchestration.react_runner.get_chat_model", lambda **kwargs: asyncio.sleep(0, result=model))
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_for_request", route)
    monkeypatch.setattr("app.agents.orchestration.react_runner.make_skill_tool", fake_tool)
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("尝试不同方法读取文档"))
    assert result.success is True
    assert [record["skill"] for record in result.records] == ["first", "second"]
    assert toolsets[0][2] == ["first", "second"]
    assert toolsets[1][1] == {"first"}
    assert toolsets[1][2] == ["second"]
