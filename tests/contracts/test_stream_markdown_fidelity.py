"""流式 Markdown 保真回归：逐增量剥离工具标记时**不能吞掉结构空白**。

用户报的现象：回答里 ``## 总体概况`` 粘成 ``##总体概况``、换行消失、代码块围栏错位。
根因是 ``strip_tool_markup()`` 结尾的 ``.strip()`` 在**每个增量**上执行——分块边界
一落到空白处就被吃掉。这里锁死修复后的行为。
"""

from __future__ import annotations

from lumi_orch.protocol import (
    ModelStreamProtocolParser,
    TextToolStripper,
    chunk_to_events,
    strip_tool_markup,
)

MARKDOWN_CHUNKS = [
    "##",
    " 总体概况",
    "\n\n",
    "- **1.",
    " `README.md`**",
    "\n",
    "```python",
    "\n",
    "if __name__ == '__main__':",
    "\n",
    "```",
    "\n",
]


def test_per_chunk_strip_keeps_markdown_whitespace():
    raw = "".join(MARKDOWN_CHUNKS)
    cleaned = "".join(strip_tool_markup(chunk) for chunk in MARKDOWN_CHUNKS)
    assert cleaned == raw, "逐增量剥离不得改动任何空白/换行"
    assert "## 总体概况" in cleaned
    assert "if __name__ == '__main__':" in cleaned


def test_plain_text_is_returned_untouched():
    for text in ("  只有缩进的正文  ", "\n\n", "   ", "if __name__ == '__main__':\n"):
        assert strip_tool_markup(text) == text


def test_tool_markup_is_still_removed():
    # 真的剥掉了标记时，只删除被匹配的片段，不顺手去掉两侧空白。
    text = '正文前 <workspace_read path="a.py">payload</workspace_read> 正文后'
    cleaned = strip_tool_markup(text)
    assert "workspace_read" not in cleaned
    assert cleaned == "正文前  正文后", f"只删片段、保留两侧空白：{cleaned!r}"


def test_full_stream_pipeline_preserves_markdown_bytes():
    """端到端复刻 orchestrator 的直答流链路，逐增量保真。

    链路与 ``app/services/orchestrator.py`` 的直答分支一致：
    ``TextToolStripper.feed`` → ``ModelStreamProtocolParser.feed`` →
    ``chunk_to_events`` → ``strip_tool_markup``。这里是用户实际看到的那条路，
    比单测更接近现场：``## 总体概况`` 的缩进、空行、代码围栏、``__name__``
    都不允许被任何一层改动。
    """
    stripper = TextToolStripper()
    parser = ModelStreamProtocolParser()
    out: list[str] = []

    def _consume(piece: str) -> None:
        for chunk in parser.feed(piece):
            for event in chunk_to_events(chunk):
                if event["type"] != "delta":
                    continue
                clean = strip_tool_markup(str(event.get("content") or ""))
                if clean:
                    out.append(clean)

    for raw in MARKDOWN_CHUNKS:
        for piece in stripper.feed(raw):
            _consume(piece)
    for piece in stripper.flush():
        _consume(piece)
    for chunk in parser.finalize():
        for event in chunk_to_events(chunk):
            if event["type"] == "delta":
                clean = strip_tool_markup(str(event.get("content") or ""))
                if clean:
                    out.append(clean)

    joined = "".join(out)
    assert joined == "".join(MARKDOWN_CHUNKS), f"链路改动了解答文本：{joined!r}"
    assert "## 总体概况" in joined
    assert "__name__" in joined
    assert "\\_" not in joined, "后端不得给解答文本插入 Markdown 转义"


def test_markdown_block_before_tool_call_stays_in_the_answer():
    """用户报告形态的第二个坑：正文块紧挨工具调用时不得被搬进过程通道。

    标题 / 列表项 / 代码围栏 / 空行同样"短且以换行结尾"，曾和"我来读取…"一样被
    整块改道 thinking —— 结果标题和围栏从回答里消失（只在过程气泡里看得到）。
    """
    cases = (
        ("## 总体概况\n", "\n\n正文", "## 总体概况\n\n\n正文"),
        ("- 第一项\n", "\n- 第二项", "- 第一项\n\n- 第二项"),
        ("```python\n", "print(1)\n```\n", "```python\nprint(1)\n```\n"),
        ("\n\n", "\n\n正文", "\n\n\n\n正文"),
        ("结果如下：\n", "正文", "结果如下：\n正文"),
    )
    for head, tail, expected in cases:
        stripper = TextToolStripper()
        pieces: list[str] = []
        for chunk in (head, '<workspace_read path="a.py">', "payload", "</workspace_read>", tail):
            pieces.extend(stripper.feed(chunk))
        pieces.extend(stripper.flush())
        joined = "".join(pieces)
        process = stripper.drain_process()
        assert process == "", f"{head!r} 不该进过程通道：{process!r}"
        assert joined == expected, f"{head!r} 被搬离正文：answer={joined!r}"


def test_progress_preamble_leaves_no_raw_markup_in_the_process_channel():
    """开场白改道过程通道时，原始 XML/DSML 标记不得搭车泄漏。"""
    stripper = TextToolStripper()
    pieces: list[str] = []
    for chunk in ("我来读取这份 PPT 的内容。\n", '<workspace_read path="a.pptx">', "幻灯片 1", "</workspace_read>\n", "总结如下。"):
        pieces.extend(stripper.feed(chunk))
    pieces.extend(stripper.flush())
    process = stripper.drain_process()
    assert "我来读取这份 PPT 的内容。" in process
    assert "<" not in process and "workspace_read" not in process, f"过程通道泄漏标记：{process!r}"
    assert "".join(pieces) == "\n总结如下。"


def test_unclosed_tag_does_not_swallow_following_text():
    # 逐增量剥离时，闭合标签可能落在后面的增量里。此时绝不能把开场标签之后的
    # 内容一起吞掉；跨增量的成对标记由有状态的 TextToolStripper 负责。
    text = '正文前 <workspace_read path="a.py"> 正文后'
    cleaned = strip_tool_markup(text)
    assert cleaned == text, f"未闭合的开场标签不得吞掉同增量正文：{cleaned!r}"


def test_stripper_removes_straddling_tag_and_keeps_answer_text():
    stripper = TextToolStripper()
    pieces: list[str] = []
    for chunk in ("正文前", '<workspace_read path="a.py">', "payload", "</workspace_read>", "正文后"):
        pieces.extend(stripper.feed(chunk))
    pieces.extend(stripper.flush())
    joined = "".join(pieces)
    assert joined == "正文前正文后", f"跨增量标记要被剥掉且正文不丢：{joined!r}"

    # 紧随工具调用的“我来读取…”开场白属于过程通道，不污染正文——它同样不能丢。
    stripper = TextToolStripper()
    pieces = []
    for chunk in ("我先读取这份文件。\n", '<workspace_read path="a.py">', "payload", "</workspace_read>", "正文后"):
        pieces.extend(stripper.feed(chunk))
    pieces.extend(stripper.flush())
    joined = "".join(pieces)
    assert joined == "正文后", f"开场白不应进正文：{joined!r}"
    assert "我先读取这份文件。" in stripper.drain_process()


def test_stripper_stream_preserves_answer_newlines():
    stripper = TextToolStripper()
    pieces: list[str] = []
    for chunk in ("##", " 标题", "\n\n", "正文\n", "\n", "结尾"):
        pieces.extend(stripper.feed(chunk))
    pieces.extend(stripper.flush())
    joined = "".join(pieces)
    assert "## 标题" in joined, f"标题不得粘连：{joined!r}"
    assert "\n\n" in joined, f"空行必须保留：{joined!r}"
    assert joined.endswith("结尾")


def test_dsml_marker_is_not_leaked_but_noise_is_preserved():
    # 分层契约：TextToolStripper 故意把完整 DSML 标记透传给解析器（解析器要把它
    # 归一化成 tool_calls），真正保证“标记不进回答正文”的是解析器这一层。
    stripper = TextToolStripper()
    raw_pieces: list[str] = []
    for chunk in ("先说一句。\n", '<||DSML||invoke name="x"/>', "正文"):
        raw_pieces.extend(stripper.feed(chunk))
    raw_pieces.extend(stripper.flush())
    assert '<||DSML||invoke name="x"/>' in "".join(raw_pieces), "DSML 必须原样交给解析器"

    parser = ModelStreamProtocolParser()
    answer: list[str] = []
    tool_names: list[str] = []
    for chunk in ("先说一句。\n", '<||DSML||invoke name="x"/>', "正文"):
        for parsed in parser.feed(chunk):
            answer.append(parsed.answer_delta)
            tool_names.extend(call.name for call in parsed.tool_calls)
    joined = "".join(answer)
    assert "DSML" not in joined, f"标记不得进入回答正文：{joined!r}"
    assert tool_names == ["x"], f"标记必须变成工具调用：{tool_names!r}"
    assert "先说一句。" in joined and "正文" in joined

    # 另外，strip_tool_markup 这一路对 DSML 是能删干净的（正文保真）。
    assert strip_tool_markup('前<||DSML||invoke name="x"/>后') == "前后"
