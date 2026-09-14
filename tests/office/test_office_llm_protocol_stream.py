import asyncio

from app.agents.skills.base import SkillContext
from app.office import skill_utils as office_skill_utils


class _FakeLLM:
    async def chat_stream(self, _messages, **_kwargs):
        for item in (
            "我来读取这份 PPT 的内容进行梳理。\n",
            '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="workspace_read"> '
            '<｜｜DSML｜｜parameter name="path" string="true">a.pptx</｜｜DSML｜｜parameter> '
            '</｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>',
            "整理完成。",
        ):
            yield item


def test_office_skill_stream_filters_fullwidth_dsml(monkeypatch):
    async def scenario():
        outputs, notices = [], []
        monkeypatch.setattr(office_skill_utils, "LLMClient", lambda: _FakeLLM())
        ctx = SkillContext(
            user_id="u1",
            job_id="j1",
            on_output=outputs.append,
            on_notify=notices.append,
        )
        result = await office_skill_utils.office_llm(ctx, "system", "user", stream=True)
        text = "".join(outputs)
        assert result == text
        assert "DSML" not in text
        assert "workspace_read" not in text
        assert "整理完成" in text
        assert len(outputs) >= 1

    asyncio.run(scenario())


class _SentinelLLM:
    """模型直接吐出内部路由哨兵（模拟真实 provider 的续写）。"""

    def __init__(self, chunks):
        self._chunks = chunks

    async def chat_stream(self, _messages, **_kwargs):
        for item in self._chunks:
            yield item


def _stream_office_llm(monkeypatch, chunks):
    outputs: list[str] = []

    async def scenario():
        monkeypatch.setattr(office_skill_utils, "LLMClient", lambda: _SentinelLLM(chunks))
        ctx = SkillContext(user_id="u1", job_id="j1", on_output=outputs.append)
        return await office_skill_utils.office_llm(ctx, "system", "user", stream=True)

    return asyncio.run(scenario()), "".join(outputs)


def test_route_sentinel_split_across_deltas_never_leaks_prefix(monkeypatch):
    """哨兵被 provider 切成 "[[" / "ROUTE_UPGRADE" / "_RAG]]" 时，"[[" 也不能漏出去。"""
    result, text = _stream_office_llm(
        monkeypatch, ["[[", "ROUTE_UPGRADE", "_RAG]]"]
    )
    assert text == ""
    assert "[[" not in text
    # 返回值仍保留完整标记，交给上层识别并转换成受控的重新路由结果。
    assert "ROUTE_UPGRADE_RAG" in result


def test_route_sentinel_discards_buffered_lead_text(monkeypatch):
    """哨兵前只有很短一段缓冲文本：一并丢弃，不能出现半截标记。"""
    result, text = _stream_office_llm(
        monkeypatch, ["[[ROUTE_UPGRADE_RAG]]"]
    )
    assert text == ""
    assert result.strip().endswith(office_skill_utils.ROUTE_SENTINEL_PREFIX + "RAG]]")


def test_route_sentinel_discards_short_lead_text(monkeypatch):
    """哨兵前有极短文本（不足缓冲长度）时不流出，也不产生半截标记。"""
    _result, text = _stream_office_llm(
        monkeypatch, ["马", "上", "[[ROUTE_UPGRADE_RAG]]"]
    )
    assert "[[" not in text and "ROUTE_UPGRADE" not in text


def test_long_lead_text_streams_before_sentinel_without_marker(monkeypatch):
    """哨兵前有长文本时，长文本应正常流出，且不含任何标记碎片。"""
    lead = "下面给出结论：" + "内容" * 40
    result, text = _stream_office_llm(
        monkeypatch, [lead, "\n[[ROUTE_UPGRADE_RAG]]"]
    )
    assert "[[" not in text and "ROUTE_UPGRADE" not in text
    assert "下面给出结论" in text


def test_normal_text_is_not_swallowed_by_sentinel_buffering(monkeypatch):
    """正常文本必须完整流出（尾部缓冲不能吃掉尾部内容）。"""
    chunks = ["这是一份", "完整", "的普通回答。"]
    result, text = _stream_office_llm(monkeypatch, chunks)
    assert text == "".join(chunks)
    assert result == text
