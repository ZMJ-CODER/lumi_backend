import asyncio

from langchain_core.messages import AIMessage

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.react_runner import OfficeReactRunner
from app.agents.roles.react import ReactStepAgent
from app.agents.core.base import WorkerContext
from app.agents.skills.base import Tool, SkillResult
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

    def bind_tools(self, tools):
        self.tools = tools
        return _Bound(self)

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return self.replies.pop(0)


class _Echo(Tool):
    name = "react_echo"
    description = "react test echo"
    scenes = ["office"]
    parameters_schema = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, params, context=None):
        return SkillResult(success=True, output=f"echo:{params.get('text')}")


def test_office_react_runs_one_tool_per_round(monkeypatch):
    ToolRegistry.register(_Echo())
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
        "app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace",
        test_capabilities,
    )
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("完成动态任务"))
    assert result.success is True
    assert result.content == "动态任务完成"
    assert [r["skill"] for r in result.records] == ["react_echo"]


def test_office_react_preserves_reasoning_payload_after_tool_call(monkeypatch):
    """Thinking-mode providers require reasoning_content on the tool-result turn."""
    ToolRegistry.register(_Echo())
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
        "app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace",
        capabilities,
    )
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("完成动态任务"))
    assert result.success is True
    replayed = next(message for message in model.calls[1] if isinstance(message, AIMessage))
    assert replayed.additional_kwargs["reasoning_content"] == "tool selection reasoning"


def test_m3_planner_requires_abstract_capability_contract(monkeypatch):
    from app.agents.orchestration.planner import LlmPlanner
    from app.agents.orchestration.tca import ComplexityLevel

    planner = LlmPlanner()

    async def fake_structured(*_args, **_kwargs):
        return {
            "plan": "需要根据中间结果处理",
            "abstract_tasks": [{
                "id": "r1", "name": "动态处理", "instruction": "分析销售下滑",
                "profile": {"goal": "ANALYZE", "required_sources": ["USER_INPUT"], "complexity": "DYNAMIC", "safety_level": "READ_ONLY"},
                "depends_on": [],
            }],
        }

    async def fake_projects(_user_id):
        return []

    monkeypatch.setattr(planner, "_call_structured_planner", fake_structured)
    monkeypatch.setattr(planner, "_list_projects", fake_projects)
    tree = asyncio.run(planner.plan_for_level(
        ComplexityLevel.M3, "u1", "分析销售下滑原因并给出建议", "office",
    ))
    assert len(tree.nodes) == 1
    assert tree.nodes[0].agent == "direct_llm"


def test_react_worker_requires_instruction():
    result = asyncio.run(ReactStepAgent().execute(
        TaskNode(id="r1", agent="react_step"), WorkerContext(user_id="u1", job_id="j1")
    ))
    assert result["error_code"] == "INVALID_ARGS"


def test_react_runner_autonomous_mode_extends_system_contract(monkeypatch):
    model = _Model([AIMessage(content="无需工具，任务完成")])
    monkeypatch.setattr(
        "app.agents.orchestration.react_runner.get_chat_model",
        lambda **kwargs: asyncio.sleep(0, result=model),
    )
    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(name="react_echo", description="react test echo", category="office")]
    monkeypatch.setattr(
        "app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace",
        capabilities,
    )
    result = asyncio.run(OfficeReactRunner(
        user_id="u1", job_id="j1", autonomous_mode=True,
    ).run("实现功能并确保能运行"))
    assert result.success is True
    system = "\n".join(str(message.content) for message in model.calls[0] if getattr(message, "content", None))
    assert "滚动执行任务" in system
    assert "遇到错误先分析错误类别" in system


def test_react_is_only_planned_for_m3():
    from app.agents.orchestration.planning.normalizer import enforce_react_complexity_policy

    node = TaskNode(id="r1", agent="react_step", name="生成摘要", params={"instruction": "生成摘要"})
    enforce_react_complexity_policy([node], "m2")
    assert node.agent == "direct_llm"
    assert node.metadata["react_blocked_by_complexity"] == "m2"


def test_react_kept_for_m3():
    from app.agents.orchestration.planning.normalizer import enforce_react_complexity_policy

    node = TaskNode(id="r1", agent="react_step", name="开放分析", params={"instruction": "开放分析"})
    enforce_react_complexity_policy([node], "m3")
    assert node.agent == "react_step"


def test_planner_rejects_removed_stage_domain_path(monkeypatch):
    from app.agents.orchestration.planner import LlmPlanner
    from app.agents.orchestration.tca import ComplexityLevel

    planner = LlmPlanner()

    async def fake_structured(*_args, **_kwargs):
        return {
            "plan": "先查公开资料，再汇总",
            "stages": [
                {"stage_id": "research", "domain": "research", "goal": "获取官方资料", "mode": "read_only", "depends_on": []},
                {"stage_id": "writeup", "domain": "writing", "goal": "生成对比说明", "mode": "direct", "depends_on": ["research"]},
            ],
            "tasks": [],
        }

    async def fake_projects(_user_id):
        return []

    monkeypatch.setattr(planner, "_call_structured_planner", fake_structured)
    monkeypatch.setattr(planner, "_list_projects", fake_projects)
    tree = asyncio.run(planner.plan_for_level(
        ComplexityLevel.M3, "比较两个公开方案", "u1", "office",
    ))
    assert tree.nodes == []
    assert tree.error_code == "PLANNER_EMPTY"


def test_react_worker_injects_prior_results(monkeypatch):
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
                "prior_context": {"step-1": {"instruction": "生成摘要", "result": "摘要内容"}},
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
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace", route)
    monkeypatch.setattr("app.agents.orchestration.react_runner.make_skill_tool", fake_tool)
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("尝试不同方法读取文档"))
    assert result.success is True
    assert [record["skill"] for record in result.records] == ["first", "second"]
    assert toolsets[0][2] == ["first", "second"]
    assert toolsets[1][1] == {"first"}
    assert toolsets[1][2] == ["second"]
    assert len(result.selection_traces) == 3
    assert result.selection_traces[0]["selection_round"] == 1
    assert result.selection_traces[0]["model_called"] == "first"
    assert result.selection_traces[1]["model_called"] == "second"


def test_office_react_compiles_candidate_boundary_from_registry_contract(monkeypatch):
    model = _Model([AIMessage(content="不需要工具，直接完成")])

    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(
            name="react_echo", category="office", domain="document", version="2.0.0",
            use_when=["读取已授权测试内容"], do_not_use_when=["无需外部内容时直接回答"],
        )]

    monkeypatch.setattr("app.agents.orchestration.react_runner.get_chat_model", lambda **kwargs: asyncio.sleep(0, result=model))
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace", capabilities)
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("不需要外部内容"))
    assert result.success is True
    assert "候选工具选择边界" in str(model.calls[0][0].content)
    assert "react_echo@2.0.0" in str(model.calls[0][0].content)


def test_react_blocks_write_before_read(monkeypatch):
    model = _Model([
            AIMessage(content="", tool_calls=[{"name": "Edit", "args": {"file_path": "x", "content": "x"}, "id": "w1"}]),
        AIMessage(content="已停止"),
    ])
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_chat_model", lambda **kwargs: asyncio.sleep(0, result=model))
    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(name="Edit", description="edit", category="office")]
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace", capabilities)
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1", autonomous_mode=True).run("修改文件"))
    assert result.success is True
    assert result.records[0]["success"] is False


def test_react_requires_read_for_same_target(monkeypatch):
    model = _Model([
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {"file_path": "a.py"}, "id": "r1"}]),
        AIMessage(content="", tool_calls=[{"name": "Write", "args": {"file_path": "b.py", "content": "x"}, "id": "w1"}]),
        AIMessage(content="已停止"),
    ])
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_chat_model", lambda **kwargs: asyncio.sleep(0, result=model))
    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(name="Read", description="read", category="office"), ToolCapability(name="Write", description="write", category="office")]
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace", capabilities)
    async def fake_tool(name, **kwargs):
        class Tool:
            async def ainvoke(self, args):
                result = SkillResult(success=name == "Read", output="content" if name == "Read" else "")
                await kwargs["on_result"](result)
                return result.output
        return Tool()
    monkeypatch.setattr("app.agents.orchestration.react_runner.make_skill_tool", fake_tool)
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1", autonomous_mode=True).run("修改代码"))
    assert [record["skill"] for record in result.records] == ["Read", "Write"]
    assert result.records[-1]["success"] is False


def test_react_result_contains_execution_metrics(monkeypatch):
    model = _Model([AIMessage(content="完成")])
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_chat_model", lambda **kwargs: asyncio.sleep(0, result=model))
    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(name="react_echo", description="echo", category="office")]
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace", capabilities)
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1", autonomous_mode=True).run("完成"))
    assert result.metrics["autonomous_mode"] is True
    assert result.metrics["rounds"] == 0


def test_domain_first_starts_with_only_discovery_primitive(monkeypatch):
    model = _Model([AIMessage(content="", tool_calls=[{"name": "discover_domain", "args": {"domain": "network", "reason": "查公开资料"}, "id": "d1"}]), AIMessage(content="已进入网络域")])
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_chat_model", lambda **kwargs: asyncio.sleep(0, result=model))
    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(name="web_search", description="search", category="network", domain="research", parameters={"type": "object", "properties": {}})]
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace", capabilities)
    runner = OfficeReactRunner(user_id="u1", job_id="j1", domain_first=True)
    result = asyncio.run(runner.run("找公开资料"))
    assert result.success is True
    assert runner.toolsets[0] == ["search_tools", "discover_domain"]


def test_invalid_params_turns_into_clarification(monkeypatch):
    model = _Model([AIMessage(content="", tool_calls=[{"name": "needs_arg", "args": {}, "id": "x1"}]), AIMessage(content="请提供必要信息")])
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_chat_model", lambda **kwargs: asyncio.sleep(0, result=model))
    async def capabilities(*_args, **_kwargs):
        return [ToolCapability(name="needs_arg", description="arg", category="office", parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]})]
    monkeypatch.setattr("app.agents.orchestration.react_runner.get_office_react_capabilities_with_trace", capabilities)
    async def fake_tool(name, **kwargs):
        class Tool:
            async def ainvoke(self, args):
                return ""
        return Tool()
    monkeypatch.setattr("app.agents.orchestration.react_runner.make_skill_tool", fake_tool)
    result = asyncio.run(OfficeReactRunner(user_id="u1", job_id="j1").run("处理任务"))
    assert result.success is True
