"""统一模型输出协议防火墙/解析层的回归测试。"""

from __future__ import annotations

from lumi_orch.protocol import (
    ModelStreamProtocolParser,
    normalize_native_tool_calls,
    parse_one_shot,
)


def _feed_all(parser: ModelStreamProtocolParser, text: str):
    chunks = []
    for part in (text,):  # 一次喂入便于断言分片边界；分片场景另测
        chunks.extend(parser.feed(part))
    chunks.extend(parser.finalize())
    return chunks


def test_native_function_calling_normalization():
    calls = normalize_native_tool_calls([{
        "id": "call_1",
        "function": {"name": "workspace_read", "arguments": '{"path": "a.md"}'},
    }])
    assert len(calls) == 1
    assert calls[0].name == "workspace_read"
    assert calls[0].arguments == {"path": "a.md"}
    assert calls[0].protocol == "native_function_calling"


def test_attribute_dsml_invocation():
    text = '<||DSML||invoke name="workspace_read" arguments=\'{"path": "a.md"}\'/>'
    calls, warnings = parse_one_shot(text)
    assert len(calls) == 1
    assert calls[0].name == "workspace_read"
    # feed/finalize 拆分方式与一次性一致
    parser = ModelStreamProtocolParser()
    chunks = _feed_all(parser, text)
    tool_calls = [c for chunk in chunks for c in chunk.tool_calls]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "workspace_read"
    assert tool_calls[0].arguments == {"path": "a.md"}
    assert tool_calls[0].protocol == "dsml_attribute"
    assert not warnings
    # DSML 原文绝不出现在 answer/process
    assert all("<||DSML||" not in (chunk.answer_delta or chunk.process_delta) for chunk in chunks)


def test_nested_param_dsml_invocation():
    text = (
        '<||DSML||invoke name="workspace_read">'
        '<||DSML||参数 name="path" string="true">智慧物业管理系统毕业设计答辩.pptx</||DSML||参数>'
        "</||DSML||invoke>"
    )
    parser = ModelStreamProtocolParser()
    chunks = _feed_all(parser, text)
    tool_calls = [c for chunk in chunks for c in chunk.tool_calls]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "workspace_read"
    assert tool_calls[0].arguments == {"path": "智慧物业管理系统毕业设计答辩.pptx"}
    assert tool_calls[0].protocol == "dsml_nested"


def test_lead_natural_language_becomes_process_on_tool():
    text = (
        "我先查看一下这份PPT的内容。"
        '<||DSML||invoke name="workspace_read" arguments=\'{"path": "a.pptx"}\'/>'
        "后面正文"
    )
    parser = ModelStreamProtocolParser()
    chunks = _feed_all(parser, text)
    processes = "".join(chunk.process_delta for chunk in chunks)
    answers = "".join(chunk.answer_delta for chunk in chunks)
    tool_calls = [c for chunk in chunks for c in chunk.tool_calls]
    assert "我先查看一下这份PPT的内容。" in processes
    assert len(tool_calls) == 1
    assert "后面正文" in answers
    # DSML 原文与 arguments 均不进入最终文本通道
    assert "<||DSML||" not in processes + answers
    assert "workspace_read" not in processes + answers


def test_pure_text_streams_as_answer_without_tools():
    parser = ModelStreamProtocolParser(flush_window=32)
    chunks = _feed_all(parser, "这是一段普通回答，不包含任何工具协议标记。")
    assert any(chunk.answer_delta for chunk in chunks)
    assert not any(chunk.tool_calls for chunk in chunks)
    assert not any(chunk.warnings for chunk in chunks)


def test_partial_start_is_held_until_closed():
    parser = ModelStreamProtocolParser(flush_window=64)
    first = parser.feed('我先看看。\n<||DSML||invo')
    # 未闭合：正文扣留在协议缓冲，不把半截协议当正文发出
    assert not any(chunk.answer_delta for chunk in first)
    assert any(chunk.protocol_pending for chunk in first)
    second = parser.feed('ke name="workspace_read" arguments=\'{"path":"a.txt"}\'/>')
    third = parser.feed(" 之后")
    chunks = second + third + parser.finalize()
    tools = [c for chunk in chunks for c in chunk.tool_calls]
    answers = "".join(chunk.answer_delta for chunk in chunks)
    processes = "".join(chunk.process_delta for chunk in chunks)
    assert len(tools) == 1
    assert tools[0].name == "workspace_read"
    assert "我先看看。" in processes
    assert "之后" in answers
    assert "<||DSML||" not in processes + answers


def test_unclosed_protocol_warns_and_does_not_leak():
    text = "准备读取。\n<||DSML||invoke name=\"workspace_read\""
    parser = ModelStreamProtocolParser()
    chunks = _feed_all(parser, text)
    assert any(chunk.warnings for chunk in chunks)
    assert not any(chunk.tool_calls for chunk in chunks)
    leaked = "".join(chunk.answer_delta + chunk.process_delta for chunk in chunks)
    assert "<||DSML||" not in leaked


def test_parse_one_shot_collects_calls_and_warnings():
    text = (
        '<||DSML||召唤 name="workspace_read" arguments=\'{"path": "1.md"}\'/>'
        + "正文"
        + '<||DSML||invoke name="workspace_search" arguments=\'{"query": "a"}\'/>'
    )
    calls, warnings = parse_one_shot(text)
    assert [call.name for call in calls] == ["workspace_read", "workspace_search"]
    assert warnings == []
