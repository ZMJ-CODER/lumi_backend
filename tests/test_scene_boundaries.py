"""普通聊天与办公 DAG 的职责边界回归测试。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.orchestrator import Orchestrator
from app.services.scene_manager import get_scene_system_prompt
from app.services.response_format import OFFICE_RESPONSE_FORMAT_COMPACT
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


def test_office_prepare_does_not_prequery_knowledge(monkeypatch):
    """办公模式是否检索必须由 DAG 节点决定，不能在规划前隐式执行。"""
    orch = Orchestrator.__new__(Orchestrator)
    calls = []

    async def context(*args):
        return []

    async def summary(*args):
        return None

    async def prompt(*args):
        return "office"

    async def query(*args):
        calls.append(args)
        return "不应出现", [{"title": "无关来源"}]

    async def append(*args):
        return None

    monkeypatch.setattr(orch, "get_context", context)
    monkeypatch.setattr(orch, "get_conversation_summary", summary)
    monkeypatch.setattr(orch, "_get_system_prompt", prompt)
    monkeypatch.setattr(orch, "_retrieve_knowledge", query)
    monkeypatch.setattr(orch, "append_context", append)

    prepared = asyncio.run(
        orch._prepare_chat("u1", "c1", "查询当前时间", "office", None, [], retrieve_knowledge=False)
    )
    assert calls == []
    assert prepared["citations"] == []


def test_office_prompt_requires_structured_markdown_delivery():
    prompt = get_scene_system_prompt("office")

    assert "回复排版" in prompt
    assert "## 核心结论" in prompt
    assert "Markdown" in prompt


def test_compact_office_format_covers_single_step_delivery():
    assert "Markdown" in OFFICE_RESPONSE_FORMAT_COMPACT
    assert "## 注意事项" in OFFICE_RESPONSE_FORMAT_COMPACT
    assert "- [ ]" in OFFICE_RESPONSE_FORMAT_COMPACT


def test_office_document_request_with_open_app_uses_free_planning():
    result = classify(
        "分析这些文档，完成后打开 Excel 应用",
        [{"doc_id": "d1", "filename": "scores.csv"}],
    )

    assert result["task_type"] == "free"


def test_office_direct_generation_bypasses_dag(monkeypatch):
    """办公模式的纯文本创作也应走直接模型回复，而非任务编排。"""
    orch = Orchestrator.__new__(Orchestrator)
    calls = {"chat": 0, "office": 0}

    async def resolve(content, attachments):
        return content

    async def prepare(*args, **kwargs):
        return {"is_first": False, "messages": [{"role": "system", "content": "office"}, {"role": "user", "content": "作文"}], "citations": []}

    async def no_images(*args, **kwargs):
        return []

    async def title(*args, **kwargs):
        return "existing"

    async def chat(*args, **kwargs):
        calls["chat"] += 1
        return "作文正文"

    async def office(*args, **kwargs):
        calls["office"] += 1
        return "错误路径", [], []

    async def finalize(*args, **kwargs):
        return None

    monkeypatch.setattr(orch, "_resolve_transcript", resolve)
    monkeypatch.setattr(orch, "_prepare_chat", prepare)
    monkeypatch.setattr(orch, "_load_image_data_uris", no_images)
    monkeypatch.setattr(orch, "get_conversation_title", title)
    monkeypatch.setattr(orch, "_call_llm_auto", chat)
    monkeypatch.setattr(orch, "_run_office_job", office)
    monkeypatch.setattr(orch, "_finalize_reply", finalize)

    result = asyncio.run(orch.handle_message("u1", "c1", "写一篇作文，800 字以上", scene="office"))

    assert result["content"] == "作文正文"
    assert result["steps"] == []
    assert calls == {"chat": 1, "office": 0}


def test_office_direct_generation_stream_bypasses_job_events(monkeypatch):
    """纯文本办公回复在 SSE 中只输出正文和 done，不创建办公 job。"""
    orch = Orchestrator.__new__(Orchestrator)
    calls = {"office": 0}

    async def resolve(content, attachments):
        return content

    async def prepare(*args, **kwargs):
        return {"is_first": False, "messages": [{"role": "system", "content": "office"}, {"role": "user", "content": "作文"}], "citations": []}

    async def no_images(*args, **kwargs):
        return []

    async def title(*args, **kwargs):
        return "existing"

    async def text_stream(*args, **kwargs):
        yield {"type": "delta", "content": "第一段"}
        yield {"type": "delta", "content": "第二段"}

    async def office_stream(*args, **kwargs):
        calls["office"] += 1
        yield {"type": "job", "job_id": "wrong-path"}

    async def finalize(*args, **kwargs):
        return None

    monkeypatch.setattr(orch, "_resolve_transcript", resolve)
    monkeypatch.setattr(orch, "_prepare_chat", prepare)
    monkeypatch.setattr(orch, "_load_image_data_uris", no_images)
    monkeypatch.setattr(orch, "get_conversation_title", title)
    monkeypatch.setattr(orch, "_stream_llm_auto", text_stream)
    monkeypatch.setattr(orch, "_stream_office_job", office_stream)
    monkeypatch.setattr(orch, "_finalize_reply", finalize)

    async def scenario():
        return [event async for event in orch.handle_message_stream("u1", "c1", "写一篇作文", scene="office")]

    events = asyncio.run(scenario())
    assert [event["type"] for event in events] == ["delta", "delta", "done"]
    assert events[-1]["content"] == "第一段第二段"
    assert calls == {"office": 0}


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


def test_office_stream_emits_plan_revision_step(monkeypatch):
    from app.agents.orchestration import orchestrator as agent_orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    initial = SimpleNamespace(
        job_id="job-plan",
        status=JobStatus.RUNNING,
        nodes=[],
        routing={"plan_revision": 1},
        created_at=1.0,
    )
    revised = SimpleNamespace(
        job_id="job-plan",
        status=JobStatus.COMPLETED,
        nodes=[],
        routing={
            "plan_revision": 2,
            "plan_change_reason": "读取方法失败，改用脚本导出。",
        },
        result={"final_answer": "已完成"},
        error=None,
        created_at=1.0,
    )

    async def submit(*args, **kwargs):
        return initial

    async def get_job(*args, **kwargs):
        return revised

    monkeypatch.setattr(agent_orchestrator, "submit_job", submit)
    monkeypatch.setattr(agent_orchestrator, "get_job", get_job)

    async def scenario():
        stream = orch._stream_office_job("u1", "c1", "task", [], None, [])
        assert (await anext(stream))["type"] == "job"
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(scenario())
    assert event["type"] == "step"
    assert event["step"]["id"] == "plan-revision-2"
    assert event["step"]["output"] == "读取方法失败，改用脚本导出。"


def test_office_stream_finishes_with_explicit_error_when_job_snapshot_disappears(monkeypatch):
    """SSE 已交付 job_id 后 Redis 丢快照时，不能无限等待再让前端显示中断。"""
    from app.agents.orchestration import orchestrator as agent_orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    initial = SimpleNamespace(
        job_id="missing-job",
        status=JobStatus.RUNNING,
        nodes=[],
        routing={},
        result=None,
        error=None,
        created_at=1.0,
        updated_at=1.0,
    )

    async def submit(*args, **kwargs):
        return initial

    async def get_job(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_orchestrator, "submit_job", submit)
    monkeypatch.setattr(agent_orchestrator, "get_job", get_job)

    async def scenario():
        stream = orch._stream_office_job("u1", "c1", "task", [], None, [])
        assert (await anext(stream))["type"] == "job"
        # sleep is implementation detail; make the three guarded retries immediate.
        import app.services.orchestrator as service_orchestrator

        async def no_sleep(*args, **kwargs):
            return None

        monkeypatch.setattr(service_orchestrator.asyncio, "sleep", no_sleep)
        events = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    events = asyncio.run(scenario())
    assert events[0]["type"] == "step"
    assert events[0]["step"]["status"] == "failed"
    assert events[1]["type"] == "delta"
    assert "任务状态已丢失" in events[1]["content"]
