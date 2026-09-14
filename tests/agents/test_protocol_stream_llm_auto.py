"""_stream_llm_auto 直答协议流回归测试（实时增量 + 残留剥离）。

验证：
  - 模型增量即时转 delta（工具/DSML/告警/过程句不外发、不执行、不二次调用）；
  - DSML / workspace_read 类 XML 永不进入 delta 正文；
  - 纯文本回复单次调用。
"""

from __future__ import annotations

import asyncio

from app.services.orchestrator import Orchestrator


class _FakeLLM:
    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = []

    async def chat_stream(self, messages, **kwargs):
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.rounds) - 1)
        for delta in self.rounds[index]:
            yield delta


def _orchestrator_with(fake_llm) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch._llm = fake_llm
    return orch


def test_closed_dsml_not_leaked_and_not_resent():
    async def scenario():
        fake = _FakeLLM([
            [
                "我先核对一下。",
                '<||DSML||invoke name="workspace_read" arguments=\'{"path": "a.txt"}\'/>',
                "之后再继续回答",
            ],
        ])
        orch = _orchestrator_with(fake)
        events = [
            event
            async for event in orch._protocol_llm_stream(
                [{"role": "user", "content": "hi"}], scene="chat"
            )
        ]
        combined = "".join(
            str(e.get("content") or "") for e in events if e["type"] == "delta"
        )
        assert "<||DSML||" not in combined
        assert "<" not in combined
        # 不执行工具、不二次调用
        assert not any(e["type"] == "tool" for e in events)
        assert len(fake.calls) == 1
        assert "之后再继续回答" in combined

    asyncio.run(scenario())


def test_unclosed_protocol_no_warning_and_no_leak():
    async def scenario():
        fake = _FakeLLM([
            ["准备处理。\n<||DSML||invoke name=\"workspace_read\""],
            ["（残余未闭合）"],
        ])
        orch = _orchestrator_with(fake)
        events = [
            event
            async for event in orch._protocol_llm_stream(
                [{"role": "user", "content": "读一下文件"}], scene="chat"
            )
        ]
        assert not any(e["type"] in {"warning", "tool"} for e in events)
        assert len(fake.calls) == 1
        leaked = "".join(
            str(e.get("content") or "") for e in events if e["type"] == "delta"
        )
        assert "<||DSML||" not in leaked
        assert "<" not in leaked

    asyncio.run(scenario())


def test_plain_text_single_call():
    async def scenario():
        fake = _FakeLLM([["你好，这是一段普通回复。"]])
        orch = _orchestrator_with(fake)
        events = [
            event
            async for event in orch._protocol_llm_stream(
                [{"role": "user", "content": "hi"}], scene="chat"
            )
        ]
        assert len(fake.calls) == 1
        joined = "".join(e["content"] for e in events if e["type"] == "delta")
        assert joined == "你好，这是一段普通回复。"

    asyncio.run(scenario())


def test_fullwidth_dsml_tool_calls_wrapper_never_leaks():
    async def scenario():
        fake = _FakeLLM([[
            "我来读取这份 PPT 的内容进行梳理。\n",
            '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="workspace_read"> '
            '<｜｜DSML｜｜parameter name="path" string="true">智慧物业管理系统毕业设计答辩.pptx'
            '</｜｜DSML｜｜parameter> </｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>',
            "整理完成。",
        ]])
        orch = _orchestrator_with(fake)
        events = [event async for event in orch._protocol_llm_stream(
            [{"role": "user", "content": "读取 PPT"}], scene="office"
        )]
        text = "".join(str(e.get("content") or "") for e in events)
        assert "DSML" not in text
        assert "workspace_read" not in text
        assert "整理完成" in text
        assert len(fake.calls) == 1

    asyncio.run(scenario())


def test_fullwidth_dsml_split_across_provider_chunks_never_leaks():
    async def scenario():
        raw = (
            "我来读取这份 PPT 的内容进行梳理。\n"
            '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="workspace_read"> '
            '<｜｜DSML｜｜parameter name="path" string="true">智慧物业管理系统毕业设计答辩.pptx'
            '</｜｜DSML｜｜parameter> </｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>'
        )
        pieces = [raw[i:i + 3] for i in range(0, len(raw), 3)] + ["后续回答。"]
        fake = _FakeLLM([pieces])
        orch = _orchestrator_with(fake)
        events = [event async for event in orch._protocol_llm_stream(
            [{"role": "user", "content": "读取 PPT"}], scene="office"
        )]
        text = "".join(str(e.get("content") or "") for e in events)
        assert "DSML" not in text
        assert "workspace_read" not in text
        assert "后续回答" in text

    asyncio.run(scenario())
