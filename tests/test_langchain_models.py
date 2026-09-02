import asyncio

from app.agents.langchain import models
from app.core.config import settings
from langchain_core.messages import HumanMessage


def test_default_model_does_not_send_reasoning_effort(monkeypatch):
    captured = {}

    class FakeChatModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def fake_config(scene, user_id=None):
        return {"model": "deepseek-v4-flash", "base_url": "https://example.test/v1", "api_key": "key"}

    monkeypatch.setattr(models, "CompatibleChatOpenAI", FakeChatModel)
    monkeypatch.setattr(models, "get_llm_config", fake_config)
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT_MODELS", "")

    asyncio.run(models.get_chat_model(scene="office", user_id="u1", reasoning_effort="high"))

    assert captured["model"] == "deepseek-v4-flash"
    assert "reasoning_effort" not in captured


def test_whitelisted_model_can_send_reasoning_effort(monkeypatch):
    captured = {}

    class FakeChatModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def fake_config(scene, user_id=None):
        return {"model": "deepseek-v4-flash", "base_url": "https://example.test/v1", "api_key": "key"}

    monkeypatch.setattr(models, "CompatibleChatOpenAI", FakeChatModel)
    monkeypatch.setattr(models, "get_llm_config", fake_config)
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT_MODELS", "deepseek-v4-flash")

    asyncio.run(models.get_chat_model(scene="office", user_id="u1", reasoning_effort="high"))

    assert captured["reasoning_effort"] == "high"


def test_actual_openai_payload_omits_reasoning_effort_by_default(monkeypatch):
    async def fake_config(scene, user_id=None):
        return {"model": "DeepSeek V4 Flash", "base_url": "https://example.test/v1", "api_key": "key"}

    monkeypatch.setattr(models, "get_llm_config", fake_config)
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT_MODELS", "")

    chat_model = asyncio.run(
        models.get_chat_model(scene="office", user_id="u1", reasoning_effort="high")
    )
    payload = chat_model._get_request_payload([HumanMessage(content="ping")])

    assert chat_model.model_name == "deepseek-v4-flash"
    assert "reasoning_effort" not in payload


def test_official_deepseek_v4_normalizes_legacy_v1_and_enables_thinking(monkeypatch):
    async def fake_config(scene, user_id=None):
        return {
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "key",
        }

    monkeypatch.setattr(models, "get_llm_config", fake_config)
    chat_model = asyncio.run(
        models.get_chat_model(scene="office", user_id="u1", reasoning_effort="high")
    )
    payload = chat_model._get_request_payload([HumanMessage(content="ping")])

    assert str(chat_model.openai_api_base) == "https://api.deepseek.com"
    assert payload["reasoning_effort"] == "high"
    assert payload["extra_body"] == {"thinking": {"type": "enabled"}}


def test_llm_http_proxy_is_forwarded_to_chat_model(monkeypatch):
    captured = {}

    class FakeChatModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def fake_config(scene, user_id=None):
        return {"model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com", "api_key": "key"}

    monkeypatch.setattr(models, "CompatibleChatOpenAI", FakeChatModel)
    monkeypatch.setattr(models, "get_llm_config", fake_config)
    monkeypatch.setattr(settings, "LLM_HTTP_PROXY", "http://127.0.0.1:7890")
    asyncio.run(models.get_chat_model(scene="office", user_id="u1"))

    assert captured["openai_proxy"] == "http://127.0.0.1:7890"


def test_system_proxy_is_used_when_explicit_proxy_is_empty(monkeypatch):
    captured = {}

    class FakeChatModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def fake_config(scene, user_id=None):
        return {"model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com", "api_key": "key"}

    monkeypatch.setattr(models, "CompatibleChatOpenAI", FakeChatModel)
    monkeypatch.setattr(models, "get_llm_config", fake_config)
    monkeypatch.setattr(settings, "LLM_HTTP_PROXY", "")
    monkeypatch.setattr(models, "resolve_http_proxy", lambda explicit=None: "http://127.0.0.1:7890")
    asyncio.run(models.get_chat_model(scene="office", user_id="u1"))

    assert captured["openai_proxy"] == "http://127.0.0.1:7890"
