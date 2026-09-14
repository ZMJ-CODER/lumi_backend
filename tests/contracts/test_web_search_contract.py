"""联网是受控工具，不因时间词或私有上下文被后端强制触发。"""

import asyncio

import pytest

from app.core.config import settings
from app.services.orchestrator import (
    _append_chat_tool_contract,
    _needs_chat_tool_graph,
    _requires_fresh_web_data,
)
from app.services.web_search import WebSearchUnavailableError, web_search_required


def test_fresh_web_detection_never_forces_network_from_markers():
    assert _requires_fresh_web_data("今天上海天气怎么样") is False
    assert _requires_fresh_web_data("我今天的待办还有哪些") is False
    assert _requires_fresh_web_data("latest exchange rate") is False
    assert _requires_fresh_web_data("请解释唐朝长安城的历史") is False


def test_web_decision_prompt_is_conservative():
    from app.services.orchestrator import _WEB_DECISION_PROMPT

    assert "不确定时不要调用" in _WEB_DECISION_PROMPT
    assert "上传附件" in _WEB_DECISION_PROMPT
    assert "今天/当前/实时" in _WEB_DECISION_PROMPT


def test_chat_tool_entry_is_not_a_web_trigger():
    assert _needs_chat_tool_graph("为什么天空是蓝色的？") is True
    assert _needs_chat_tool_graph("我今天的待办还有哪些") is False
    assert _needs_chat_tool_graph("帮我总结刚上传的附件") is False
    assert _needs_chat_tool_graph("你好") is False


def test_tool_contract_is_injected_into_existing_system_message():
    messages = _append_chat_tool_contract(
        [{"role": "system", "content": "base"}, {"role": "user", "content": "问题"}],
        web_search_preferred=False,
    )
    assert messages[0]["content"].startswith("base")
    assert "不确定时不要调用" in messages[0]["content"]


def test_required_web_search_never_returns_empty_when_key_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    with pytest.raises(WebSearchUnavailableError, match="API Key"):
        asyncio.run(web_search_required("今天上海天气"))
