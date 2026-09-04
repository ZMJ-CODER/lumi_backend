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


def test_structured_planner_failure_returns_explicit_planning_error(monkeypatch):
    planner = LlmPlanner()

    async def no_structured(*args, **kwargs):
        return None

    monkeypatch.setattr(planner, "_call_structured_planner", no_structured)
    tree = asyncio.run(planner.plan("u1", "查询"))
    # 工作流不能悄悄替换为关键词规则图，否则会改变用户任务的真实语义。
    assert tree.nodes == []
    assert tree.error_code == "PLANNER_EMPTY"


def test_datetime_request_is_tool_assisted_before_workflow_planning():
    from app.agents.orchestration.intent import OfficeDispatch, classify_office_dispatch

    assert classify_office_dispatch("请查询当前日期和时间，并用一行说明。") == OfficeDispatch.TOOL_ASSISTED


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


def test_office_dispatch_keeps_general_questions_out_of_the_job_runtime():
    from app.agents.orchestration.intent import OfficeDispatch, classify_office_dispatch

    assert classify_office_dispatch("怎么写好周报？") == OfficeDispatch.DIRECT
    assert classify_office_dispatch("分析下面这段发布说明并列出风险") == OfficeDispatch.DIRECT
    assert classify_office_dispatch("查询公司内部报销政策") == OfficeDispatch.TOOL_ASSISTED
    assert classify_office_dispatch("精确计算 (18*7)/3") == OfficeDispatch.TOOL_ASSISTED
    assert classify_office_dispatch("把会议纪要保存为 Word 文档") == OfficeDispatch.WORKFLOW


def test_rule_planner_compiles_common_read_only_tools_without_llm():
    async def scenario():
        from app.agents.orchestration.planner import RulePlanner

        planner = RulePlanner()
        for query, agent, tool in (
            ("帮我查一下天气", "web_research", None),
            ("请计算 12*7", "atomic_step", "Calculator"),
            ("查询知识库里的报销规则", "retrieval", None),
        ):
            tree = await planner.plan("u1", query, scene="office")
            assert len(tree.nodes) == 1
            assert tree.nodes[0].agent == agent
            if tool:
                assert tree.nodes[0].params["preferred_tool"] == tool

    asyncio.run(scenario())


def test_explicit_multi_calculation_compiles_parallel_nodes_and_delivery():
    """复合算式不得降级为 Calculator 的一次最长片段调用。"""
    from app.agents.orchestration.planner import RulePlanner

    request = "请分别计算以下三项，并最后汇总为表格：A=(485*17+230)/5；B=1024*0.85；C=(9999-1234)/7。"
    tree = asyncio.run(RulePlanner().plan("u1", request, scene="office"))
    calculators = [node for node in tree.nodes if node.params.get("preferred_tool") == "Calculator"]
    assert len(calculators) == 3
    assert {node.params["inputs"]["expression"] for node in calculators} == {
        "(485*17+230)/5", "1024*0.85", "(9999-1234)/7",
    }
    assert all(node.metadata["preserve_dependencies"] is True for node in calculators)
    assert len(tree.nodes) == 3
    assert all(node.depends_on == [] for node in calculators)


def test_multi_calculation_accepts_natural_leading_calculation_phrase():
    """首项紧跟“计算 A=”时也必须完整物化为三个独立计算节点。"""
    from app.agents.orchestration.planner import _multi_calculation_tree

    tree = _multi_calculation_tree(
        "请分别计算 A=(485*17+230)/5；B=1024*0.85；C=(9999-1234)/7，并最后汇总为表格。"
    )
    assert tree is not None
    calculators = [node for node in tree.nodes if node.params.get("preferred_tool") == "Calculator"]
    assert len(calculators) == 3
    assert {node.params["inputs"]["expression"] for node in calculators} == {
        "(485*17+230)/5", "1024*0.85", "(9999-1234)/7",
    }


def test_calculator_shortcut_rejects_compound_request_instead_of_dropping_items():
    from app.agents.orchestration.planner import _deterministic_read_tool_tree

    assert _deterministic_read_tool_tree("请计算 A=1+1；B=2+2，并汇总") is None


def test_calculator_shortcut_accepts_one_expression_with_delivery_only_suffix():
    from app.agents.orchestration.planner import _deterministic_read_tool_tree

    tree = _deterministic_read_tool_tree("请计算（12873×47－912）÷13，只返回精确结果。")
    assert tree is not None
    assert tree.nodes[0].params["inputs"]["expression"] == "(12873*47-912)/13"


def test_calculator_shortcut_accepts_natural_result_only_suffix():
    """自然语言“告诉我结果”不能让确定性算式误入 LLM 规划。"""
    from app.agents.orchestration.planner import _deterministic_read_tool_tree

    tree = _deterministic_read_tool_tree("请精确计算 ((24680-1357)*19)/7，只返回结果。")
    assert tree is not None
    assert tree.nodes[0].params["preferred_tool"] == "Calculator"
    assert tree.nodes[0].params["inputs"]["expression"] == "((24680-1357)*19)/7"


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


def test_workflow_planner_auth_error_is_returned_instead_of_falling_back(monkeypatch):
    planner = LlmPlanner()

    async def missing_key(*args, **kwargs):
        raise RuntimeError("Missing credentials. Please pass an api_key")

    monkeypatch.setattr("app.agents.langchain.planning.invoke_structured_planner", missing_key)
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
