"""模型目录 + 用户级 LLM 配置解析测试."""

import asyncio

from app.core.config import settings
from app.core.llm_config import _env_fallback, _resolve_user_cfg, get_llm_config
from app.core.model_catalog import (
    PROVIDER_BASE_URLS,
    find_model,
    get_model_catalog,
    normalize_model_id,
    normalize_byok_base_url,
)
from app.services.orchestrator import (
    _chat_model_override,
    _get_chat_model_override,
    _needs_memory_fact_retrieval,
    _should_retrieve_chat_knowledge,
)


def test_catalog_contains_env_configured_models():
    catalog = get_model_catalog()
    ids = [m["id"] for m in catalog]
    assert ids == ["deepseek-v4-flash", "deepseek-v4-pro", "qwen-turbo"]
    for m in catalog:
        assert m["context_window"] > 0
        assert "multimodal" in m
        assert "supports_reasoning_effort" in m
        assert m["price_input_per_million"] >= 0
    assert find_model("deepseek-v4-flash")["provider"] == "deepseek"
    assert find_model("deepseek-v4-pro")["provider"] == "deepseek"
    assert find_model("qwen-turbo")["provider"] == "qwen"
    assert find_model("not-exist") is None


def test_normalize_model_id_uses_provider_id_for_legacy_display_name():
    assert normalize_model_id("DeepSeek V4 Flash") == "deepseek-v4-flash"
    assert normalize_model_id(" deepseek-v4-flash ") == "deepseek-v4-flash"
    assert normalize_model_id("vendor/custom-model") == "vendor/custom-model"


def test_resolve_user_cfg_byok():
    cfg = asyncio.run(
        _resolve_user_cfg(
            {"provider": "deepseek", "model": "my-model", "byok": True, "reasoning_effort": "high"},
            provider=None,
        )
    )
    assert cfg is not None
    assert cfg["byok"] is True
    assert cfg["api_key"] == ""  # BYOK key 不落库，走请求头
    assert cfg["base_url"] == PROVIDER_BASE_URLS["deepseek"]
    assert cfg["model"] == "my-model"
    assert cfg["reasoning_effort"] == "high"


def test_resolve_user_cfg_normalizes_legacy_builtin_display_name():
    cfg = asyncio.run(
        _resolve_user_cfg(
            {"provider": "deepseek", "model": "DeepSeek V4 Flash", "byok": False},
            provider=None,
        )
    )
    assert cfg is not None
    assert cfg["model"] == "deepseek-v4-flash"


def test_resolve_user_cfg_custom_byok_endpoint():
    cfg = asyncio.run(
        _resolve_user_cfg(
            {
                "provider": "custom",
                "model": "vendor/chat-model",
                "base_url": "https://gateway.example.com/v1/",
                "byok": True,
            },
            provider=None,
        )
    )
    assert cfg is not None
    assert cfg["base_url"] == "https://gateway.example.com/v1"
    assert cfg["api_key"] == ""


def test_byok_endpoint_validation_rejects_private_or_credentials():
    assert normalize_byok_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    for value in ("http://127.0.0.1:11434/v1", "https://user:pass@example.com/v1"):
        try:
            normalize_byok_base_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid endpoint: {value}")


def test_resolve_user_cfg_server_key():
    cfg = asyncio.run(
        _resolve_user_cfg(
            {"provider": "qwen", "model": settings.QWEN_MODEL, "byok": False},
            provider=None,
        )
    )
    assert cfg is not None
    assert cfg["byok"] is False
    assert cfg["api_key"] == settings.QWEN_API_KEY  # 服务端 .env 密钥
    assert cfg["base_url"] == settings.QWEN_BASE_URL


def test_get_llm_config_prefers_user_layer(monkeypatch):
    async def fake_user_cfg(user_id):
        assert user_id == "u-123"
        return {"provider": "deepseek", "model": settings.DEEPSEEK_MODEL, "byok": False}

    import app.services.user_llm_config as ulc

    monkeypatch.setattr(ulc, "get_user_llm_config", fake_user_cfg)
    cfg = asyncio.run(get_llm_config(scene="chat", user_id="u-123"))
    assert cfg["source"] == "user"
    assert cfg["model"] == settings.DEEPSEEK_MODEL
    assert cfg["base_url"] == settings.DEEPSEEK_BASE_URL


def test_chat_env_fallback_uses_text_model():
    assert _env_fallback(scene="chat")["model"] == settings.QWEN_MODEL


def test_chat_knowledge_retrieval_is_on_demand():
    assert not _should_retrieve_chat_knowledge("帮我写一封邮件", [], None)
    assert _should_retrieve_chat_knowledge("总结我刚才上传的文档", [], None)
    assert _should_retrieve_chat_knowledge("这是什么", [{"type": "file", "url": "/uploads/a.txt"}], None)
    assert _should_retrieve_chat_knowledge("这是什么", [], "精炼检索词")


def test_memory_fact_retrieval_requires_explicit_history_reference():
    assert not _needs_memory_fact_retrieval("帮我润色这段文字", None)
    assert _needs_memory_fact_retrieval("按我上次的偏好继续", None)


def test_user_selected_chat_model_is_not_overridden(monkeypatch):
    async def user_cfg(*args, **kwargs):
        return {"source": "user", "model": "chosen-model"}

    import app.services.orchestrator as orch_module

    monkeypatch.setattr(orch_module, "get_llm_config", user_cfg)
    assert asyncio.run(_get_chat_model_override("chat", "fast", None, "u-123")) is None


def test_server_chat_override_uses_qwen_without_byok(monkeypatch):
    monkeypatch.setattr(settings, "QWEN_MODEL", "qwen-turbo")
    monkeypatch.setattr(settings, "QWEN_BASE_URL", "https://qwen.example/v1")
    monkeypatch.setattr(settings, "QWEN_API_KEY", "qwen-key")
    monkeypatch.setattr(settings, "DEEPSEEK_MODEL", "deepseek-chat")

    fast = _chat_model_override("chat", "fast", None)
    think = _chat_model_override("chat", "think", None)

    assert fast and fast["model"] == "qwen-turbo"
    assert fast["base_url"] == "https://qwen.example/v1"
    assert think and think["model"] == "qwen-turbo"
    assert _chat_model_override("chat", "fast", "byok-key") is None
