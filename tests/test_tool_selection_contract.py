"""Zero-execution regression cases for model-facing Skill selection contracts."""

import asyncio

import pytest

from app.agents.skills.base import Skill
from app.agents.skills.contract_lint import lint_skill_contracts
from app.agents.skills.executor import (
    get_chat_capabilities_for_request,
    get_chat_capabilities_with_trace,
    select_capabilities_with_trace,
)
from app.agents.skills.registry import SkillRegistry


@pytest.fixture(autouse=True)
def _skills():
    from app.agents.skills import loader

    SkillRegistry.clear()
    loader.unload_skill_plugins()
    loader.load_skill_plugins()
    yield
    loader.unload_skill_plugins()
    SkillRegistry.clear()


@pytest.mark.parametrize(
    ("query", "expected", "must_not_call"),
    [
        ("现在几点？", {"get_datetime"}, {"web_search", "query_knowledge"}),
        ("请联网搜索本周 AI 政策并给网页来源", {"web_search"}, {"query_knowledge"}),
        ("根据我的知识库说明报销规则", {"query_knowledge"}, {"web_search"}),
        ("Python 的 GIL 是什么", set(), {"web_search", "query_knowledge", "get_datetime"}),
        ("我今天的待办还有哪些", set(), {"web_search", "get_datetime"}),
    ],
)
def test_chat_tool_selection_contract(query, expected, must_not_call):
    names = {item.name for item in asyncio.run(get_chat_capabilities_for_request(query))}
    assert expected <= names
    assert names.isdisjoint(must_not_call)


def test_core_tool_descriptions_include_negative_boundaries():
    tools = {
        item.name: item.to_tool_definition()["function"]["description"]
        for item in asyncio.run(get_chat_capabilities_for_request("请联网搜索本周 AI 政策并给网页来源"))
    }
    assert "web_search" in tools
    assert "不要用于" in tools["web_search"]
    assert "get_datetime" in tools["web_search"]


@pytest.mark.parametrize(
    ("case_id", "query", "expected_candidate", "must_not_inject"),
    [
        ("current-time", "现在几点？", "get_datetime", {"web_search", "query_knowledge"}),
        ("public-source", "麻烦联网查一下最近的人工智能监管规定，要网页出处", "web_search", {"query_knowledge"}),
        ("knowledge-base", "帮我从知识库找一下出差报销的上限", "query_knowledge", {"web_search"}),
    ],
)
def test_tool_candidate_recall_fixture(case_id, query, expected_candidate, must_not_inject):
    """Layer 1 eval: expected tool must enter the injected Top-K pool."""
    selection = asyncio.run(get_chat_capabilities_with_trace(query))
    names = {item.name for item in selection.capabilities}
    assert expected_candidate in names, case_id
    assert names.isdisjoint(must_not_inject), case_id
    candidate = next(item for item in selection.candidates if item["name"] == expected_candidate)
    assert candidate["version"]
    assert isinstance(candidate["score"], float)


def test_selection_trace_keeps_only_safe_routing_metadata():
    selection = asyncio.run(get_chat_capabilities_with_trace("请联网搜索本周 AI 政策并给网页来源"))
    trace = selection.to_metadata(selection_round=2)
    assert trace["scene"] == "chat"
    assert trace["selection_round"] == 2
    assert "query" not in trace
    assert trace["routing_mode"] in {"semantic", "lexical_fallback"}
    assert all(
        set(item) == {"name", "version", "score", "bootstrap", "availability_hint"}
        for item in trace["injected_candidates"]
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("帮我算一下 (12873 × 47 - 912) ÷ 13，保留两位小数", "calculator"),
        ("帮我打开记事本", "open_app"),
    ],
)
def test_chat_tool_candidate_recall_for_deterministic_and_desktop_actions(query, expected):
    names = {item.name for item in asyncio.run(get_chat_capabilities_for_request(query))}
    assert expected in names


def test_same_intent_recall_is_consistent_between_chat_and_office():
    cases = [
        ("帮我算一下 12873 * 47", "calculator"),
        ("帮我打开记事本", "open_app"),
        ("请联网搜索本周 AI 政策并给网页来源", "web_search"),
    ]
    for query, expected in cases:
        chat = {item.name for item in asyncio.run(get_chat_capabilities_for_request(query))}
        office = {item.name for item in asyncio.run(select_capabilities_with_trace(query, "office")).capabilities}
        assert expected in chat
        assert expected in office


def test_contract_lint_rejects_invalid_bootstrap_date_and_dangling_handoff():
    class BadSkill(Skill):
        name = "bad"
        handoff_to = ["missing"]
        bootstrap_until = "not-a-date"
        bootstrap_intents = ["测试"]

        async def execute(self, params, context=None):  # pragma: no cover - never executes
            raise NotImplementedError

    errors = lint_skill_contracts([BadSkill()])
    assert any("不存在" in error for error in errors)
    assert any("YYYY-MM-DD" in error for error in errors)


def test_bootstrap_only_promotes_a_skill_for_declared_intent():
    class NewSkill(Skill):
        name = "new_weather"
        version = "1.0.0"
        scenes = ["chat"]
        bootstrap_intents = ["天气", "气温"]
        bootstrap_until = "2099-01-01"

        async def execute(self, params, context=None):  # pragma: no cover - selection only
            raise NotImplementedError

    SkillRegistry.register(NewSkill())
    weather = asyncio.run(select_capabilities_with_trace("帮我看天气", "chat", limit=1))
    generic = asyncio.run(select_capabilities_with_trace("给我讲个笑话", "chat", limit=1))
    chat_weather = asyncio.run(get_chat_capabilities_with_trace("帮我看天气", limit=1))
    assert weather.capabilities[0].name == "new_weather"
    assert weather.candidates[0]["bootstrap"] is True
    assert all(item["name"] != "new_weather" for item in generic.candidates)
    assert [item.name for item in chat_weather.capabilities] == ["new_weather"]
