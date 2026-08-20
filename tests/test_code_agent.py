"""code_agent + 规划器代码任务路由测试."""

import asyncio
import uuid

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.planner import LlmPlanner, RulePlanner
from app.agents.orchestration.review import LlmReviewHook
from app.agents.core.base import WorkerContext
from app.agents.roles.code.agent import CodeAgent


def _register_code_stub(monkeypatch):
    """code agent 已被 AGENT_DISABLED 屏蔽，测试里注册一个同名 stub 满足规划器路由."""
    from app.agents.core.base import WorkerAgent
    from app.agents.core.registry import AgentRegistry

    class _Stub(WorkerAgent):
        name = "code"
        description = "stub"

        async def execute(self, node, ctx):
            return {"success": True}

    if AgentRegistry.get("code") is None:
        AgentRegistry.register(_Stub())


def _node(**kw):
    defaults = {"id": "n1", "name": "code", "agent": "code"}
    defaults.update(kw)
    return TaskNode(**defaults)


def _ctx():
    return WorkerContext(user_id=str(uuid.uuid4()), job_id="j1")


def test_code_agent_read_generate_write(monkeypatch):
    agent = CodeAgent()
    calls = []

    async def fake_locate(project_id, instruction, ctx):
        return {"path": "src/main.py"}

    async def fake_generate(ctx, instruction, path, original, project_files=None):
        assert original == "old code"
        return "new code"

    async def fake_run_skill(skill, params, ctx):
        calls.append((skill, params))
        if skill == "read_project_file":
            return {"success": True, "content": "old code"}
        return {"success": True, "content": "written"}

    monkeypatch.setattr(agent, "_locate", fake_locate)
    monkeypatch.setattr(agent, "_generate", fake_generate)
    monkeypatch.setattr(agent, "run_skill", fake_run_skill)

    node = _node(params={"project_id": "p1", "instruction": "给 main.py 添加注释"})
    result = asyncio.run(agent.execute(node, _ctx()))

    assert result["success"] is True
    assert calls[0][0] == "read_project_file"
    assert calls[1][0] == "write_project_file"
    assert calls[0][1]["path"] == "src/main.py"
    assert calls[1][1]["path"] == "src/main.py"
    assert calls[1][1]["content"] == "new code"
    # 质检层可审查内容
    assert result["new_content"] == "new code"
    assert result["path"] == "src/main.py"


def test_code_agent_requires_params(monkeypatch):
    agent = CodeAgent()
    result = asyncio.run(agent.execute(_node(params={}), _ctx()))
    assert result["success"] is False
    assert result["error_code"] == "INVALID_ARGS"


def test_code_agent_locate_failure(monkeypatch):
    agent = CodeAgent()

    async def fake_locate(project_id, instruction, ctx):
        return {}

    monkeypatch.setattr(agent, "_locate", fake_locate)
    node = _node(params={"project_id": "p1", "instruction": "改某个文件"})
    result = asyncio.run(agent.execute(node, _ctx()))
    assert result["success"] is False
    assert result["error_code"] == "EXEC_ERROR"


def test_rule_planner_routes_code_task(monkeypatch):
    _register_code_stub(monkeypatch)
    # 显式 project_id → code 节点
    tree = asyncio.run(RulePlanner().plan("u1", "帮我实现登录功能", project_id="p1"))
    assert tree.nodes[0].agent == "code"
    assert tree.nodes[0].params["project_id"] == "p1"

    # 请求中包含已注册项目名 → code 节点
    class FakeProject:
        def __init__(self):
            self.id = uuid.UUID(int=2)
            self.name = "订单系统"

    import app.services.project_index as pi

    async def fake_list(session, user_id):
        return [FakeProject()]

    monkeypatch.setattr(pi, "list_projects", fake_list)
    tree = asyncio.run(RulePlanner().plan("u1", "在订单系统里添加导出功能"))
    assert tree.nodes[0].agent == "code"
    assert str(tree.nodes[0].params["project_id"]) == str(FakeProject().id)

    # 无项目匹配 → 检索节点
    tree = asyncio.run(RulePlanner().plan("u1", "唐朝长安城有多少人"))
    assert tree.nodes[0].agent == "retrieval"


def test_code_agent_passes_byok_key_to_llm(monkeypatch):
    """CodeAgent 内部 LLM 调用应携带任务级 BYOK key."""
    captured = {}

    async def fake_chat(self, messages, **kwargs):
        captured["api_key"] = kwargs.get("api_key")
        return "generated code"

    import app.core.llm as llm_mod

    monkeypatch.setattr(llm_mod.LLMClient, "chat", fake_chat)
    agent = CodeAgent()
    result = asyncio.run(
        agent._generate(
            WorkerContext(user_id=str(uuid.uuid4()), job_id="j1", llm_api_key="sk-agent"),
            "加注释",
            "src/main.py",
            "old code",
        )
    )
    assert result == "generated code"
    assert captured.get("api_key") == "sk-agent"


def test_llm_planner_builds_task_tree(monkeypatch):
    _register_code_stub(monkeypatch)
    planner = LlmPlanner()

    async def fake_structured(user_id, request, context, llm_api_key):
        return {
            "tasks": [
                {"id": "t1", "name": "定位", "agent": "retrieval", "params": {"query": "订单"}},
                {"id": "t2", "name": "修改", "agent": "code", "params": {"instruction": "加导出功能"}},
            ],
            "clarification": "",
        }

    async def fake_list(user_id):
        return [{"id": "p1", "name": "订单系统"}]

    monkeypatch.setattr(planner, "_call_structured_planner", fake_structured)
    monkeypatch.setattr(planner, "_list_projects", fake_list)
    tree = asyncio.run(planner.plan("u1", "在订单系统里加导出"))
    assert [n.agent for n in tree.nodes] == ["retrieval", "code"]
    assert tree.nodes[1].params["project_id"] == "p1"  # 单项目自动补 project_id
    assert tree.nodes[1].params["instruction"] == "加导出功能"


def test_llm_planner_clarification(monkeypatch):
    planner = LlmPlanner()

    async def fake_structured(user_id, request, context, llm_api_key):
        return {"tasks": [], "clarification": "你想在哪个项目里修改？"}

    async def fake_list(user_id):
        return []

    monkeypatch.setattr(planner, "_call_structured_planner", fake_structured)
    monkeypatch.setattr(planner, "_list_projects", fake_list)
    tree = asyncio.run(planner.plan("u1", "帮我改代码"))
    assert tree.nodes == []
    assert tree.clarification == "你想在哪个项目里修改？"


def test_llm_planner_falls_back_on_failure(monkeypatch):
    _register_code_stub(monkeypatch)
    planner = LlmPlanner()

    async def no_structured(*args, **kwargs):
        return None

    monkeypatch.setattr(planner, "_call_structured_planner", no_structured)

    # 回退到 RulePlanner：请求含项目名 → code 节点
    import app.services.project_index as pi

    class FakeProject:
        def __init__(self):
            self.id = uuid.UUID(int=7)
            self.name = "订单系统"

    async def fake_list(session, user_id):
        return [FakeProject()]

    monkeypatch.setattr(pi, "list_projects", fake_list)
    tree = asyncio.run(planner.plan("u1", "在订单系统里加导出"))
    assert tree.nodes[0].agent == "code"
    assert str(tree.nodes[0].params["project_id"]) == str(FakeProject().id)


def test_llm_planner_uses_clarification_answer(monkeypatch):
    planner = LlmPlanner()
    captured = {}

    async def fake_structured(user_id, request, context, llm_api_key):
        captured["context"] = context
        return {"tasks": [], "clarification": ""}

    monkeypatch.setattr(planner, "_call_structured_planner", fake_structured)
    asyncio.run(planner.plan("u1", "帮我改代码", clarification_answer="订单系统"))
    assert "用户补充说明：订单系统" in captured["context"]


def test_rule_planner_uses_clarification_answer(monkeypatch):
    """LLM 规划失败回退规则时，澄清回答也应参与项目名匹配."""
    _register_code_stub(monkeypatch)
    import app.services.project_index as pi

    class FakeProject:
        def __init__(self):
            self.id = uuid.UUID(int=9)
            self.name = "订单系统"

    async def fake_list(session, user_id):
        return [FakeProject()]

    monkeypatch.setattr(pi, "list_projects", fake_list)
    tree = asyncio.run(
        RulePlanner().plan("u1", "帮我加导出功能", clarification_answer="订单系统")
    )
    assert tree.nodes[0].agent == "code"
    assert str(tree.nodes[0].params["project_id"]) == str(FakeProject().id)


def test_review_approves_non_code_and_missing_content():
    hook = LlmReviewHook()
    node = TaskNode(id="t1", name="x", agent="retrieval", params={})
    verdict = asyncio.run(hook.review(node, {"success": True}, _ctx()))
    assert verdict.approved is True

    code_node = TaskNode(id="t2", name="code", agent="code", params={})
    verdict = asyncio.run(hook.review(code_node, {"success": True}, _ctx()))
    assert verdict.approved is True  # 无 new_content → 放行


def test_review_llm_rejects_bad_code(monkeypatch):
    import app.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "AGENT_REVIEW_ENABLED", True)
    hook = LlmReviewHook()
    code_node = TaskNode(
        id="t2",
        name="code",
        agent="code",
        params={"instruction": "加个函数"},
    )
    result = {
        "success": True,
        "new_content": "def broken(:\n",
        "instruction": "加个函数",
        "path": "a.py",
    }

    async def fake_structured(*args, **kwargs):
        return {"approved": False, "feedback": "语法错误"}

    import app.agents.orchestration.review as review_mod

    monkeypatch.setattr(review_mod, "invoke_json_object", fake_structured)
    verdict = asyncio.run(hook.review(code_node, result, _ctx()))
    assert verdict.approved is False
    assert "语法错误" in verdict.feedback


def test_review_llm_failure_approves(monkeypatch):
    import app.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "AGENT_REVIEW_ENABLED", True)
    hook = LlmReviewHook()
    code_node = TaskNode(id="t2", name="code", agent="code", params={})
    result = {"success": True, "new_content": "def ok(): pass", "instruction": "x", "path": "a.py"}

    async def fake_structured(*args, **kwargs):
        raise RuntimeError("网络错误")

    import app.agents.orchestration.review as review_mod

    monkeypatch.setattr(review_mod, "invoke_json_object", fake_structured)
    verdict = asyncio.run(hook.review(code_node, result, _ctx()))
    assert verdict.approved is True  # 质检失败不阻塞主流程
