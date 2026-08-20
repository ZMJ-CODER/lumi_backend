"""LLM 供应商自动降级测试."""

import asyncio

import httpx
from langchain_core.messages import AIMessage

from app.core import llm as llm_mod
from app.core.llm import LLMClient


def test_is_retryable_error():
    assert LLMClient._is_retryable_error(RuntimeError("模型返回空内容"))
    req = httpx.Request("POST", "http://x")
    resp500 = httpx.Response(500, request=req)
    assert LLMClient._is_retryable_error(httpx.HTTPStatusError("e", request=req, response=resp500))
    resp401 = httpx.Response(401, request=req)
    assert LLMClient._is_retryable_error(httpx.HTTPStatusError("e", request=req, response=resp401))
    resp400 = httpx.Response(400, request=req)
    assert not LLMClient._is_retryable_error(
        httpx.HTTPStatusError("e", request=req, response=resp400)
    )
    assert LLMClient._is_retryable_error(httpx.ConnectError("no", request=req))


def test_fallback_cfg(monkeypatch):
    import app.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "LLM_FALLBACK_PROVIDER", "qwen")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_BASE_URL", "https://qwen/v1")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_API_KEY", "qkey")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_MODEL", "qwen-plus")
    cfg = LLMClient(provider="deepseek")._fallback_cfg()
    assert cfg and cfg["model"] == "qwen-plus"
    # 相同供应商 → 不降级
    monkeypatch.setattr(cfg_mod.settings, "LLM_FALLBACK_PROVIDER", "deepseek")
    assert LLMClient(provider="deepseek")._fallback_cfg() is None


def test_chat_falls_back_to_backup_provider(monkeypatch):
    import app.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "LLM_FALLBACK_PROVIDER", "qwen")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_BASE_URL", "https://qwen/v1")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_API_KEY", "qkey")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_MODEL", "qwen-plus")

    class _FakeChatModel:
        def __init__(self, fail):
            self.fail = fail

        async def ainvoke(self, messages):
            if self.fail:
                raise httpx.ConnectError("连接失败", request=httpx.Request("POST", "http://x"))
            return AIMessage(
                content="fallback ok",
                usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            )

    calls = []

    async def fake_get_model(**kwargs):
        calls.append(kwargs)
        return _FakeChatModel(fail=len(calls) == 1)

    monkeypatch.setattr(llm_mod, "get_chat_model", fake_get_model)

    captured = {}

    async def fake_record(*args, **kw):
        captured["model"] = args[2] if len(args) > 2 else None

    monkeypatch.setattr(llm_mod, "record_usage", fake_record)

    client = LLMClient(provider="deepseek")
    text = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert text == "fallback ok"
    assert len(calls) == 2  # 主失败 → 备用成功
    assert calls[1]["model"] == "qwen-plus"
    assert captured["model"] == "qwen-plus"  # 用量按实际模型记录
