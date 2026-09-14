"""缺模型凭据的**跨入口一致性**：HTTP 400 与 SSE 帧必须是同一套 code/status。

线上问题（复现步骤）：BYOK 用户（``byok=True``）不带 ``x-llm-api-key`` 发消息时，

* ``POST /api/v1/conversations/{id}/messages`` 修复后返回 400 + ``MODEL_API_KEY_MISSING``；
* ``POST /api/v1/chat/stream`` 一度返回 ``{"type":"error","status":500,
  "code":"CHAT_STREAM_INTERNAL_ERROR","message":"服务器内部错误"}``。

本文件把"同一种失败在两副面孔下必须一致"钉死：帧的 status/code 由
``app.api.v1.chat.stream_error_frame`` 统一产出，非流式由异常处理器统一产出。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.v1 import chat as chat_api
from app.api.v1 import conversations as conversations_api
from app.core.error_mapping import map_task_error
from app.core.exception_handlers import register_exception_handlers
from app.models.conversation import SendMessageRequest
from app.platform.model.llm import ModelCredentialsMissingError

_MESSAGE = "当前使用「自带密钥」，请在设置里填写模型 API Key 后重试（发消息时通过请求头 x-llm-api-key 携带）"


def _missing_key_error() -> ModelCredentialsMissingError:
    return ModelCredentialsMissingError(_MESSAGE, byok=True, base_url="https://api.deepseek.com")


# ── 1. SSE 帧：缺密钥不许退化成 500 ──────────────────────


def test_stream_error_frame_reports_missing_key_as_400():
    frame = chat_api.stream_error_frame(_missing_key_error())
    assert frame["type"] == "error"
    assert frame["status"] == 400
    assert frame["code"] == "MODEL_API_KEY_MISSING"
    assert "API Key" in frame["message"]
    assert "服务器内部错误" not in frame["message"]


def test_stream_error_frame_keeps_other_model_failures_actionable():
    cases = {
        "Error code: 402 Insufficient Balance": (402, "MODEL_INSUFFICIENT_BALANCE"),
        "401 unauthorized": (401, "MODEL_AUTH_ERROR"),
        "404 model not found": (404, "MODEL_NOT_FOUND"),
        "Missing credentials. Please pass an api_key": (400, "MODEL_API_KEY_MISSING"),
    }
    for error, expected in cases.items():
        frame = chat_api.stream_error_frame(RuntimeError(error))
        assert (frame["status"], frame["code"]) == expected, error


def test_stream_error_frame_falls_back_to_500_for_unknown_errors():
    """未知异常仍走兜底，且不把内部文本暴露给前端。"""

    class Boom(Exception):
        pass

    frame = chat_api.stream_error_frame(Boom("数据库连接失败：password=hunter2"))
    assert (frame["status"], frame["code"]) == (500, "CHAT_STREAM_INTERNAL_ERROR")
    assert frame["message"] == "服务器内部错误"
    assert "hunter2" not in frame["message"]


# ── 2. 非流式入口：异常必须冒到 API 层（400），不能被吞成"任务失败" ──


@pytest.mark.asyncio
async def test_send_message_propagates_missing_key_as_app_exception(monkeypatch):
    async def fake_lock(_conversation_id):
        class _Lock:
            async def release(self):
                return None

        return _Lock()

    async def missing_key(**kwargs):
        raise _missing_key_error()

    monkeypatch.setattr(conversations_api, "_acquire_conv_lock", fake_lock)
    monkeypatch.setattr(conversations_api, "_cancel_user_tts", lambda _user_id: None)
    monkeypatch.setattr(conversations_api.orchestrator, "handle_message", missing_key)

    request = Request({"type": "http", "method": "POST", "path": "/messages", "headers": []})
    with pytest.raises(ModelCredentialsMissingError) as caught:
        await conversations_api.send_message(
            request,
            "conv-1",
            SendMessageRequest(content="你好", scene="chat"),
            db=None,
            payload={"sub": "u-1", "role": "superadmin"},
        )
    assert caught.value.status_code == 400
    assert caught.value.error_code == "MODEL_API_KEY_MISSING"


def test_exception_handler_renders_400_with_error_code_and_byok_flag():
    """前端只依赖这三样：HTTP 400 / ``data.error_code`` / ``data.byok``。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise _missing_key_error()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == 400
    assert body["data"]["error_code"] == "MODEL_API_KEY_MISSING"
    assert body["data"]["byok"] is True
    assert body["data"]["base_url"] == "https://api.deepseek.com"


# ── 3. 办公任务提交：Job 卡在 failed 时也要给出可行动的码 ────────


def test_map_task_error_preserves_missing_key_code_for_app_exception():
    public = map_task_error(_missing_key_error())
    assert public.code == "MODEL_API_KEY_MISSING"
    assert public.retryable is False
    assert "API Key" in public.message


def test_map_task_error_preserves_missing_key_code_for_planner_error():
    """``PlannerModelError`` 只带 ``.code``（不是 AppException），同样要保留。"""

    class PlannerModelError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    public = map_task_error(PlannerModelError("MODEL_API_KEY_MISSING", _MESSAGE))
    assert public.code == "MODEL_API_KEY_MISSING"
    assert public.retryable is False


def test_map_task_error_recognises_provider_text_without_structured_code():
    public = map_task_error(RuntimeError("Missing credentials. Please pass an api_key"))
    assert public.code == "MODEL_API_KEY_MISSING"
    assert public.retryable is False


def test_map_task_error_keeps_internal_errors_internal():
    public = map_task_error(RuntimeError("some unexpected bug"))
    assert public.code != "MODEL_API_KEY_MISSING"
    assert public.retryable is False


# ── 4. 流式端点真跑一遍：error 帧必须是 400 + MODEL_API_KEY_MISSING ──


@pytest.mark.asyncio
async def test_chat_stream_endpoint_emits_missing_key_frame(monkeypatch):
    """把 ``/chat/stream`` 的异常分支真跑一遍（含 SseEventEncoder 编码）。"""

    class _Lock:
        async def release(self):
            return None

    class _Rate:
        allowed = True
        retry_after = 0

    async def fake_lock(_conversation_id):
        return _Lock()

    async def fake_rate(*args, **kwargs):
        return _Rate()

    async def failing_stream(**kwargs):
        raise _missing_key_error()
        yield  # pragma: no cover - 让函数成为 async generator

    async def no_duplicate(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_api, "_acquire_conv_lock", fake_lock)
    monkeypatch.setattr(chat_api, "consume_route_limit", fake_rate)
    monkeypatch.setattr(chat_api, "_find_duplicate", no_duplicate)
    monkeypatch.setattr(chat_api.orchestrator, "handle_message_stream", failing_stream)

    request = Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": []})
    response = await chat_api.chat_stream(
        request,
        SendMessageRequest(content="你好", scene="chat", conversation_id="c1", message_id="m1"),
        db=None,
        payload={"sub": "u-1", "role": "superadmin"},
    )

    body = ""
    async for chunk in response.body_iterator:
        body += chunk.decode() if isinstance(chunk, bytes) else chunk

    assert "MODEL_API_KEY_MISSING" in body
    assert '"status": 400' in body
    assert "CHAT_STREAM_INTERNAL_ERROR" not in body
    assert "服务器内部错误" not in body
