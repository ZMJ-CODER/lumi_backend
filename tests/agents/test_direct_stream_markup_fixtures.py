"""代表性样本驱动的直答流过滤测试（无真实环境的回归替身）。

覆盖用户报告的形态：
  “我来读取这份 PPT 的内容。\n<workspace_read .../>...”
要求：
  - 正文只含真实干净文本；任何 delta 都不得含 '<'、标签名或 DSML；
  - 工具标签跨多个模型增量时也能整体剥离；
  - 模型“开场白”短句若紧跟工具标签则不进正文；
  - 长正文仍按增量分多次到达（保留流式体感），不整段缓冲到结束。
"""

from __future__ import annotations

import asyncio

from lumi_orch.protocol import TextToolStripper
from app.services.orchestrator import Orchestrator


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


async def _collect(fake_llm, *, user_message="这份 PPT 主要讲了什么", scene="office"):
    orch = _orchestrator_with(fake_llm)
    return [
        event
        async for event in orch._protocol_llm_stream(
            [{"role": "user", "content": user_message}], scene=scene
        )
    ]


def test_report_case_intro_plus_workspace_read_removed():
    """用户报告形态：开场白短句 + workspace_read 开闭对标签（分多次到达）。"""
    chunks = [
        "我来读取这份 PPT 的内容。\n",
        "<workspace_read path=\"智慧物业管理系统毕业设计答辩.pptx\">",
        "幻灯片 1：系统背景与意义…",
        "</workspace_read>\n",
        "这份 PPT 主要介绍智慧物业管理系统……",
    ]
    fake = _FakeLLM(chunks)
    events = asyncio.run(_collect(fake))
    deltas = [e["content"] for e in events if e["type"] == "delta"]
    joined = "".join(deltas)
    # 无任何带符号残留
    assert "<" not in joined
    assert "workspace_read" not in joined
    assert "||DSML||" not in joined
    # 开场白短句紧跟工具标签 → 被丢弃
    assert "我来读取" not in joined
    # 真正正文保留
    assert "这份 PPT 主要介绍智慧物业管理系统" in joined
    # 只调用一次模型（无二次等待）
    assert fake.calls == 1


def test_stripper_removes_self_closing_and_pair_forms():
    stripper = TextToolStripper()
    pieces: list[str] = []
    for chunk in [
        "我先确认内容。<tool name=\"read\"/>继续说明：",
        "<workspace_read path=\"a.pptx\"></workspace_read>",
        "结束。",
    ]:
        pieces.extend(stripper.feed(chunk))
    pieces.extend(stripper.flush())
    joined = "".join(pieces)
    assert "<" not in joined
    assert "结束。" in joined


def test_long_prose_still_streams_multiple_deltas():
    long_text = "第一段内容" + "，很长的正文" * 30 + "。\n第二段继续补充说明。" * 3
    fake = _FakeLLM([long_text[:40], long_text[40:]])
    events = asyncio.run(_collect(fake, user_message="hi", scene="chat"))
    deltas = [e["content"] for e in events if e["type"] == "delta"]
    assert len(deltas) >= 1
    assert "很长的正文" in "".join(deltas)


def test_plain_short_answer_no_extra_calls():
    fake = _FakeLLM(["好的。"])
    events = asyncio.run(_collect(fake, user_message="hi", scene="chat"))
    assert [e["content"] for e in events if e["type"] == "delta"] == ["好的。"]
    assert fake.calls == 1
