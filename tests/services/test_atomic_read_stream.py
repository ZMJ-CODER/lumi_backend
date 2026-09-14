"""v2 ATOMIC 只读快路径后半段（stream_answer_from_context）回归测试。

契约：delta 必须来自真实 chat_stream 逐段输出；不允许先整段生成再切段模拟；
不发起工具决策/不阻塞总结。
"""

from __future__ import annotations

import asyncio

from app.services.orchestrator import Orchestrator, _resolve_workspace_tool_name


class _FakeLLM:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = 0

    async def chat_stream(self, messages, **kwargs):
        self.calls += 1
        for chunk in self.chunks:
            yield chunk


def _orchestrator_with(fake_llm) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch._llm = fake_llm
    return orch


def test_workspace_tool_alias_resolves_bare_provider_name_only_when_unambiguous():
    names = {"mcp__lumi_pc__workspace_read", "mcp__lumi_pc__workspace_search"}
    assert _resolve_workspace_tool_name("workspace_read", names) == "mcp__lumi_pc__workspace_read"
    assert _resolve_workspace_tool_name("mcp__lumi_pc__workspace_read", names) == "mcp__lumi_pc__workspace_read"
    assert _resolve_workspace_tool_name("workspace_missing", names) is None


def test_stream_answer_from_context_uses_real_chat_stream_deltas():
    async def scenario():
        fake = _FakeLLM(["这份文档主要介绍", "自动化流程的设计", "与实现。"])
        orch = _orchestrator_with(fake)
        events = [
            event
            async for event in orch.stream_answer_from_context(
                user_id="u1",
                messages=[{"role": "user", "content": "这份文档主要讲了什么"}],
                content="这份文档主要讲了什么",
            )
        ]
        deltas = [e["content"] for e in events if e["type"] == "delta"]
        assert len(deltas) == 3
        assert "".join(deltas) == "这份文档主要介绍自动化流程的设计与实现。"
        # 只有一次真实 chat_stream，且没有额外的整段重放/切段事件
        assert fake.calls == 1
        assert all(e["type"] == "delta" for e in events)

    asyncio.run(scenario())


def test_stream_answer_from_context_hides_protocol_no_retry():
    async def scenario():
        fake = _FakeLLM([
            ["我先核对。\n", '<||DSML||invoke name="workspace_read" arguments=\'{"path": "a.txt"}\'/>'],
            ["（重答不应发生）已整理如下："],
        ])
        orch = _orchestrator_with(fake)
        events = [
            event
            async for event in orch.stream_answer_from_context(
                user_id="u1",
                messages=[{"role": "user", "content": "读文件"}],
                content="读文件",
            )
        ]
        leaked = "".join(
            str(e.get("content") or "")
            for e in events
            if e["type"] in {"delta", "process"}
        )
        assert "<||DSML||" not in leaked
        assert "<" not in leaked
        assert not any(e["type"] == "tool" for e in events)
        # 直答阶段不执行工具、不触发第二次模型调用
        assert fake.calls == 1

    asyncio.run(scenario())


def test_atomic_workspace_ppt_question_reaches_final_stream_after_read():
    """PPT/document wording must enter the workspace read fast path.

    A regression previously classified this request as ordinary direct chat;
    the model's introductory sentence was then retained as process text while
    its DSML read call was stripped, leaving no final answer at all.
    """
    async def scenario():
        orch = _orchestrator_with(_FakeLLM([]))

        async def fake_read_workspace_context(**_kwargs):
            return "【资料：答辩.pptx】\n系统由三层架构组成。", [{"tool": "workspace_read"}]

        async def fake_answer_from_context(**_kwargs):
            for piece in ("这份 PPT 介绍了", "系统的三层架构", "和部署流程。"):
                yield {"type": "delta", "content": piece}

        orch.read_workspace_context = fake_read_workspace_context
        orch.stream_answer_from_context = fake_answer_from_context
        events = [
            event
            async for event in orch._stream_v2_atomic_read(
                user_id="u1",
                user_role="user",
                conversation_id="c1",
                content="根据工作区里的这份 PPT 文档回答主要内容",
                direct_messages=[{"role": "user", "content": "根据工作区里的这份 PPT 文档回答主要内容"}],
                workspace_id="ws1",
                workspace_summary="工作区已注册且可访问（当前版本 v1）。",
                llm_api_key=None,
                thinking_mode="fast",
            )
        ]
        assert [e["type"] for e in events] == ["process", "process", "delta", "delta", "delta"]
        assert "".join(e["content"] for e in events if e["type"] == "delta") == "这份 PPT 介绍了系统的三层架构和部署流程。"

    asyncio.run(scenario())
