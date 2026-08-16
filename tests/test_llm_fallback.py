"""LLM 供应商自动降级测试."""

import asyncio

import httpx

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


class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self._req = httpx.Request("POST", "http://x")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=self._req, response=httpx.Response(self.status_code, request=self._req)
            )

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, fail_first=True):
        self.calls = 0
        self.fail_first = fail_first

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise httpx.ConnectError("连接失败", request=httpx.Request("POST", url))
        return _FakeResp(
            {"choices": [{"message": {"content": "fallback ok"}}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        )


def test_chat_falls_back_to_backup_provider(monkeypatch):
    import app.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "LLM_FALLBACK_PROVIDER", "qwen")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_BASE_URL", "https://qwen/v1")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_API_KEY", "qkey")
    monkeypatch.setattr(cfg_mod.settings, "QWEN_MODEL", "qwen-plus")

    async def fake_get_llm_config(scene, provider, user_id=None):
        return {
            "base_url": "https://deepseek/v1",
            "api_key": "dkey",
            "model": "deepseek-chat",
            "timeout": 30,
        }

    fake_client = _FakeClient(fail_first=True)
    monkeypatch.setattr(llm_mod, "get_llm_config", fake_get_llm_config)
    monkeypatch.setattr(llm_mod, "AsyncClient", lambda **kw: fake_client)

    captured = {}

    async def fake_record(*args, **kw):
        captured["model"] = args[2] if len(args) > 2 else None

    monkeypatch.setattr(llm_mod, "record_usage", fake_record)

    client = LLMClient(provider="deepseek")
    text = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert text == "fallback ok"
    assert fake_client.calls == 2  # 主失败 → 备用成功
    assert captured["model"] == "qwen-plus"  # 用量按实际模型记录
