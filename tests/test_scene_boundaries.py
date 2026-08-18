"""普通聊天与办公 DAG 的职责边界回归测试。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.orchestrator import Orchestrator
from app.agents.orchestration.intent import classify
from app.agents.orchestration.models import JobStatus


def test_chat_bypasses_office_dag(monkeypatch):
    orch = Orchestrator.__new__(Orchestrator)
    calls = {"chat": 0, "office": 0}

    async def resolve(content, attachments):
        return content

    async def prepare(*args, **kwargs):
        return {"is_first": False, "messages": [{"role": "user", "content": "hi"}], "citations": []}

    async def no_images(*args, **kwargs):
        return []

    async def title(*args, **kwargs):
        return "existing"

    async def chat(*args, **kwargs):
        calls["chat"] += 1
        return "chat reply"

    async def office(*args, **kwargs):
        calls["office"] += 1
        return "office reply", [], []

    async def finalize(*args, **kwargs):
        return None

    monkeypatch.setattr(orch, "_resolve_transcript", resolve)
    monkeypatch.setattr(orch, "_prepare_chat", prepare)
    monkeypatch.setattr(orch, "_load_image_data_uris", no_images)
    monkeypatch.setattr(orch, "get_conversation_title", title)
    monkeypatch.setattr(orch, "_call_llm_auto", chat)
    monkeypatch.setattr(orch, "_run_office_job", office)
    monkeypatch.setattr(orch, "_finalize_reply", finalize)

    result = asyncio.run(orch.handle_message("u1", "c1", "hello", scene="chat"))

    assert result["content"] == "chat reply"
    assert calls == {"chat": 1, "office": 0}


def test_office_uses_short_history_budget(monkeypatch):
    orch = Orchestrator.__new__(Orchestrator)
    seen = []
    monkeypatch.setattr(settings, "LLM_HISTORY_MAX_TOKENS", 1000)
    monkeypatch.setattr(settings, "LLM_HISTORY_MAX_TOKENS_WORK", 100)
    monkeypatch.setattr(orch, "_trim_history", lambda history, budget: seen.append(budget) or [])

    orch._build_messages("chat", None, [], [], "hello", system_prompt="chat")
    orch._build_messages("office", None, [], [], "task", system_prompt="office")

    assert seen == [1000, 100]


def test_office_document_request_with_open_app_uses_free_planning():
    result = classify(
        "分析这些文档，完成后打开 Excel 应用",
        [{"doc_id": "d1", "filename": "scores.csv"}],
    )

    assert result["task_type"] == "free"


def test_office_stream_disconnect_does_not_cancel_job(monkeypatch):
    from app.agents.orchestration import orchestrator as agent_orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    job = SimpleNamespace(
        job_id="job-1",
        status=JobStatus.RUNNING,
        nodes=[],
        created_at=1.0,
    )
    cancelled = []

    async def submit(*args, **kwargs):
        return job

    async def get_job(*args, **kwargs):
        await asyncio.sleep(10)

    async def cancel(*args, **kwargs):
        cancelled.append(args)

    monkeypatch.setattr(agent_orchestrator, "submit_job", submit)
    monkeypatch.setattr(agent_orchestrator, "get_job", get_job)
    monkeypatch.setattr(agent_orchestrator, "cancel_job", cancel)

    async def scenario():
        stream = orch._stream_office_job("u1", "c1", "task", [], None, [])
        first = await anext(stream)
        assert first["type"] == "job"
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()

    asyncio.run(scenario())
    assert cancelled == []
