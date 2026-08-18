"""模型目录 + 用户级 LLM 配置解析测试."""

import asyncio

from app.core.config import settings
from app.core.llm_config import _resolve_user_cfg, get_llm_config
from app.core.model_catalog import PROVIDER_BASE_URLS, find_model, get_model_catalog


def test_catalog_contains_env_configured_models():
    catalog = get_model_catalog()
    ids = [m["id"] for m in catalog]
    expected = []
    if settings.DEEPSEEK_API_KEY and settings.DEEPSEEK_MODEL:
        expected.append(settings.DEEPSEEK_MODEL)
    if settings.QWEN_API_KEY and settings.QWEN_MODEL:
        expected.append(settings.QWEN_MODEL)
    assert ids == expected
    for m in catalog:
        assert m["context_window"] > 0
        assert "multimodal" in m
        assert "supports_reasoning_effort" in m
        assert m["price_input_per_million"] >= 0
    if settings.DEEPSEEK_API_KEY:
        assert find_model(settings.DEEPSEEK_MODEL)["provider"] == "deepseek"
    assert find_model("not-exist") is None


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
