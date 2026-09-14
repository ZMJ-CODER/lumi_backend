"""LangChain 结构化规划输出测试。"""

import asyncio

import pytest

from app.agents.langchain.planning import PlannerOutput, invoke_structured_planner
from app.agents.orchestration.planning.planner import LlmPlanner


def test_structured_planner_output_builds_task_tree(monkeypatch):
    planner = LlmPlanner()

    async def fake_structured(user_id, request, context, llm_api_key):
        return PlannerOutput.model_validate(
            {
                "plan": "先检索再处理",
                "abstract_tasks": [{"id": "t1", "name": "检索", "instruction": "查找订单信息", "profile": {"goal": "RETRIEVE", "required_sources": ["LOCAL_KNOWLEDGE"], "complexity": "ATOMIC", "safety_level": "READ_ONLY", "confidence": 0.9, "entities": {}}, "depends_on": []}],
            }
        ).model_dump()

    async def fake_list(user_id):
        return []

    monkeypatch.setattr(planner, "_call_structured_planner", fake_structured)
    monkeypatch.setattr(planner, "_list_projects", fake_list)
    tree = asyncio.run(planner.plan("u1", "查询订单"))
    assert tree.plan_text == "先检索再处理"
    assert tree.nodes[0].metadata["abstract_profile"]["goal"] == "RETRIEVE"


def test_structured_planner_failure_returns_explicit_planning_error(monkeypatch):
    planner = LlmPlanner()

    async def no_structured(*args, **kwargs):
        return None

    monkeypatch.setattr(planner, "_call_structured_planner", no_structured)
    tree = asyncio.run(planner.plan("u1", "查询"))
    # 工作流不能悄悄替换为关键词规则图，否则会改变用户任务的真实语义。
    assert tree.nodes == []
    assert tree.error_code == "PLANNER_EMPTY"


def test_office_stream_logging_uses_skill_context_correlation_id(monkeypatch):
    """Streaming a skill must not assume WorkerContext fields on SkillContext."""
    from app.agents.skills.base import SkillContext
    from app.office.skill_utils import office_llm

    class FakeLlm:
        async def chat_stream(self, messages, **kwargs):
            yield "第一段"

    emitted = []

    async def collect(text):
        emitted.append(text)

    monkeypatch.setattr("app.office.skill_utils.LLMClient", FakeLlm)
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


def test_workflow_planner_auth_error_is_returned_instead_of_falling_back(monkeypatch):
    planner = LlmPlanner()

    async def missing_key(*args, **kwargs):
        raise RuntimeError("Missing credentials. Please pass an api_key")

    monkeypatch.setattr("app.agents.langchain.planning.invoke_structured_planner", missing_key)
    tree = asyncio.run(planner.plan("u1", "汇总订单并且通知财务"))
    assert tree.nodes == []
    # 缺密钥是可行动的"去填 Key"，不能伪装成"密钥无效/登录过期"。
    assert tree.error_code == "MODEL_API_KEY_MISSING"


def test_structured_planner_uses_plain_json_without_schema_probe(monkeypatch):
    class PlainReply:
        content = '{"plan":"读取文档","abstract_tasks":[{"id":"t1","name":"读取","instruction":"读取已授权附件","profile":{"goal":"RETRIEVE","required_sources":["ATTACHED_FILE"],"complexity":"ATOMIC","safety_level":"READ_ONLY","confidence":0.9,"entities":{}},"depends_on":[]}],"clarification":""}'

    class Model:
        async def ainvoke(self, messages):
            return PlainReply()

    async def fake_model(**kwargs):
        return Model()

    monkeypatch.setattr("app.agents.langchain.planning.get_chat_model", fake_model)
    output = asyncio.run(invoke_structured_planner("规划", user_id="u1"))
    assert output.plan == "读取文档"
    assert output.abstract_tasks[0]["profile"]["goal"] == "RETRIEVE"


def test_structured_planner_surfaces_model_error(monkeypatch):
    class Model:
        async def ainvoke(self, messages):
            raise RuntimeError("402 Insufficient Balance")

    async def fake_model(**kwargs):
        return Model()

    monkeypatch.setattr("app.agents.langchain.planning.get_chat_model", fake_model)
    with pytest.raises(RuntimeError, match="402"):
        asyncio.run(invoke_structured_planner("规划", user_id="u1"))
