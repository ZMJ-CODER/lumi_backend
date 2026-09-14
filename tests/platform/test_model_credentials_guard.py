"""模型凭据守卫：空密钥必须变成**结构化 400**，而不是 ``openai.OpenAIError`` → 500。

背景（线上问题）：BYOK 用户（``byok=True``，密钥只随请求头 ``x-llm-api-key`` 携带）
在没带密钥时发消息，前端只看到"发消息失败/服务器内部错误"。真正原因有两层：

1. ``ChatOpenAI(api_key="")`` 抛 ``OpenAIError: Missing credentials`` → 全局兜底 500；
2. SSE 通道里错误只能走帧，而 ``classify_model_error`` 不认这个异常 → 帧退化成
   ``{"status": 500, "code": "CHAT_STREAM_INTERNAL_ERROR"}``。

本文件锁定第 1 层：缺少凭据必须是带 ``error_code`` / ``byok`` / ``base_url`` 的 400，
且本地/内网端点（Ollama、vLLM）**不受影响**。
"""

from __future__ import annotations

import asyncio

import pytest

from app.platform.model.llm import (
    ERROR_CODE_MODEL_API_KEY_MISSING,
    LLMClient,
    ModelCredentialsMissingError,
    _is_local_endpoint,
)

_BYOK_CFG = {
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "byok": True,
}
_SERVER_CFG = {
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "byok": False,
}


def _resolve(llm_config: dict, *, api_key: str | None = None):
    client = LLMClient()
    return asyncio.run(
        client._model(
            scene="chat",
            user_id="u-1",
            api_key=api_key,
            model=None,
            base_url=None,
            timeout=None,
            temperature=None,
            max_tokens=None,
            reasoning_effort=None,
            disable_reasoning_effort=False,
            messages=[{"role": "user", "content": "你好"}],
            llm_config=dict(llm_config),
        )
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:11434/v1",
        "http://0.0.0.0:11434/v1",
        "http://ollama.local:11434/v1",
        "http://10.0.0.5:11434/v1",
        "http://192.168.1.7:11434/v1",
    ],
)
def test_local_and_private_endpoints_allow_empty_key(base_url):
    assert _is_local_endpoint(base_url) is True


@pytest.mark.parametrize(
    "base_url",
    ["https://api.deepseek.com", "https://gateway.example.com/v1", "", "not-a-url"],
)
def test_remote_or_unparsable_endpoints_are_not_local(base_url):
    """解析不出来时按"远端"处理（保守）：宁可要求密钥，也不静默发匿名请求。"""
    assert _is_local_endpoint(base_url) is False


def test_byok_without_request_key_raises_structured_400():
    with pytest.raises(ModelCredentialsMissingError) as caught:
        _resolve(_BYOK_CFG)
    exc = caught.value
    assert exc.status_code == 400
    assert exc.code == 400
    assert exc.error_code == ERROR_CODE_MODEL_API_KEY_MISSING
    assert exc.data == {
        "error_code": ERROR_CODE_MODEL_API_KEY_MISSING,
        "byok": True,
        "base_url": "https://api.deepseek.com",
    }
    assert "x-llm-api-key" in exc.message


def test_missing_server_key_tells_operator_which_env_var():
    with pytest.raises(ModelCredentialsMissingError) as caught:
        _resolve(_SERVER_CFG)
    exc = caught.value
    assert exc.data["byok"] is False
    assert "DEEPSEEK_API_KEY" in exc.message


def test_request_key_is_accepted_even_for_byok(monkeypatch):
    sentinel = object()

    async def fake_get_chat_model(**kwargs):
        return sentinel

    monkeypatch.setattr("app.platform.model.llm.get_chat_model", fake_get_chat_model)
    model, selected_model, base_url, _role = _resolve(_BYOK_CFG, api_key="sk-request-key")
    assert model is sentinel
    assert selected_model == "deepseek-v4-flash"
    assert base_url == "https://api.deepseek.com"


def test_local_endpoint_without_key_still_builds_model(monkeypatch):
    """自建推理服务常常不校验密钥：守卫不能把它们一刀切掉。"""

    async def fake_get_chat_model(**kwargs):
        assert kwargs["api_key"] == ""
        return "local-model"

    monkeypatch.setattr("app.platform.model.llm.get_chat_model", fake_get_chat_model)
    model, _selected, base_url, _role = _resolve(
        {**_BYOK_CFG, "provider": "custom", "base_url": "http://127.0.0.1:11434/v1"}
    )
    assert model == "local-model"
    assert base_url == "http://127.0.0.1:11434/v1"
