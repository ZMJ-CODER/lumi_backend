"""LangChain 结构化规划输出测试。"""

import asyncio

import pytest

from app.agents.langchain.planning import PlannerOutput, invoke_structured_planner
from app.agents.orchestration.planner import LlmPlanner


def test_structured_planner_output_builds_task_tree(monkeypatch):
    planner = LlmPlanner()

    async def fake_structured(user_id, request, context, llm_api_key):
        return PlannerOutput.model_validate(
            {
                "plan": "先检索再处理",
                "tasks": [{"id": "t1", "name": "检索", "agent": "retrieval", "params": {"query": "订单"}}],
            }
        ).model_dump()

    async def fake_list(user_id):
        return []

    monkeypatch.setattr(planner, "_call_structured_planner", fake_structured)
    monkeypatch.setattr(planner, "_list_projects", fake_list)
    tree = asyncio.run(planner.plan("u1", "查询订单"))
    assert tree.plan_text == "先检索再处理"
    assert tree.nodes[0].agent == "retrieval"


def test_structured_planner_failure_returns_rule_planner_fallback(monkeypatch):
    planner = LlmPlanner()

    async def no_structured(*args, **kwargs):
        return None

    monkeypatch.setattr(planner, "_call_structured_planner", no_structured)
    tree = asyncio.run(planner.plan("u1", "查询"))
    # Planner 失败由上层选择确定性 RulePlanner，不再解析自由文本 JSON。
    assert tree.nodes[0].agent == "retrieval"


def test_datetime_request_uses_system_skill_without_model_planning(monkeypatch):
    planner = LlmPlanner()

    async def should_not_plan(*args, **kwargs):
        raise AssertionError("时间查询不应调用模型规划")

    monkeypatch.setattr(planner, "_call_structured_planner", should_not_plan)
    tree = asyncio.run(planner.plan("u1", "请查询当前日期和时间，并用一行说明。"))
    assert tree.nodes[0].agent == "atomic_step"
    assert tree.nodes[0].params["preferred_tool"] == "get_datetime"


def test_pure_writing_is_not_an_office_execution_request():
    """文本创作应直接交给聊天模型，不能被某个办公文体模板劫持。"""
    from app.agents.orchestration.intent import requires_office_execution

    assert not requires_office_execution("帮我写一篇以本手、妙手、俗手为题的作文，800 字以上")
    assert not requires_office_execution("写一段欢迎词，语气自然亲切")
    assert not requires_office_execution("帮我起草一封给客户的邮件正文")
    assert not requires_office_execution("生成一份季度报告")
    assert requires_office_execution("生成一份季度报告并导出为 Word 文档")


def test_common_read_only_tool_phrases_enter_office_execution():
    from app.agents.orchestration.intent import requires_office_execution

    assert requires_office_execution("现在几点")
    assert requires_office_execution("帮我查一下天气")
    assert requires_office_execution("请计算 12*7")
    assert requires_office_execution("查询知识库里的报销规则")
    assert not requires_office_execution("为什么不能查询")


def test_rule_planner_compiles_common_read_only_tools_without_llm():
    async def scenario():
        from app.agents.orchestration.planner import RulePlanner

        planner = RulePlanner()
        for query, agent, tool in (
            ("帮我查一下天气", "web_research", None),
            ("请计算 12*7", "atomic_step", "calculator"),
            ("查询知识库里的报销规则", "retrieval", None),
        ):
            tree = await planner.plan("u1", query, scene="office")
            assert len(tree.nodes) == 1
            assert tree.nodes[0].agent == agent
            if tool:
                assert tree.nodes[0].params["preferred_tool"] == tool

    asyncio.run(scenario())


def test_office_stream_logging_uses_skill_context_correlation_id(monkeypatch):
    """Streaming a skill must not assume WorkerContext fields on SkillContext."""
    from app.agents.skills.base import SkillContext
    from app.services.office_skill_utils import office_llm

    class FakeLlm:
        async def chat_stream(self, messages, **kwargs):
            yield "第一段"

    emitted = []

    async def collect(text):
        emitted.append(text)

    monkeypatch.setattr("app.services.office_skill_utils.LLMClient", FakeLlm)
    result = asyncio.run(
        office_llm(
            SkillContext(user_id="u1", scene="office", conversation_id="job-1", on_output=collect),
            "system",
            "user",
            stream=True,
        )
    )
    assert result == "第一段"
    assert emitted == ["第一段"]


def test_pattern_auth_error_is_returned_instead_of_falling_back_to_retrieval(monkeypatch):
    planner = LlmPlanner()

    async def missing_key(*args, **kwargs):
        raise RuntimeError("Missing credentials. Please pass an api_key")

    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", missing_key)
    tree = asyncio.run(planner.plan("u1", "汇总订单并且通知财务"))
    assert tree.nodes == []
    assert tree.error_code == "MODEL_AUTH_ERROR"


def test_structured_planner_uses_plain_json_without_schema_probe(monkeypatch):
    class PlainReply:
        content = '{"plan":"读取文档","tasks":[{"id":"t1","name":"读取","agent":"office_doc","params":{"doc_id":"d1","instruction":"读取","mode":"read"},"depends_on":[]}],"clarification":""}'

    class Model:
        async def ainvoke(self, messages):
            return PlainReply()

    async def fake_model(**kwargs):
        return Model()

    monkeypatch.setattr("app.agents.langchain.planning.get_chat_model", fake_model)
    output = asyncio.run(invoke_structured_planner("规划", user_id="u1"))
    assert output.plan == "读取文档"
    assert output.tasks[0].agent == "office_doc"


def test_structured_planner_surfaces_model_error(monkeypatch):
    class Model:
        async def ainvoke(self, messages):
            raise RuntimeError("402 Insufficient Balance")

    async def fake_model(**kwargs):
        return Model()

    monkeypatch.setattr("app.agents.langchain.planning.get_chat_model", fake_model)
    with pytest.raises(RuntimeError, match="402"):
        asyncio.run(invoke_structured_planner("规划", user_id="u1"))
