"""办公文档核心链路测试：格式识别 / 纯文本与 RTF 提取 / 文本与 PPT 编辑 / 撤销."""

import asyncio
from pathlib import Path

import pytest

from app.services import office_docs


def test_detect_kind():
    assert office_docs.detect_kind("a.docx") == "docx"
    assert office_docs.detect_kind("a.xlsx") == "xlsx"
    assert office_docs.detect_kind("a.pptx") == "pptx"
    assert office_docs.detect_kind("a.doc") == "doc"
    assert office_docs.detect_kind("a.pdf") == "pdf"
    assert office_docs.detect_kind("a.txt") == "text"
    assert office_docs.detect_kind("a.md") == "text"


def test_extract_full_text_formats_csv_eml_and_ics(monkeypatch, tmp_path):
    monkeypatch.setattr(office_docs, "OFFICE_DIR", tmp_path / "office")
    user = "u1"

    csv_meta = office_docs.create_session(
        user, "scores.csv", "姓名,成绩\n张三,95\n李四,88".encode("gb18030")
    )
    csv_text = office_docs.extract_full_text(user, csv_meta["doc_id"])
    assert "表头：姓名 | 成绩" in csv_text
    assert "第1行：张三 | 95" in csv_text

    eml = (
        "From: sender@example.com\r\nTo: user@example.com\r\n"
        "Subject: Test mail\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "邮件正文内容"
    ).encode("utf-8")
    eml_meta = office_docs.create_session(user, "mail.eml", eml)
    eml_text = office_docs.extract_full_text(user, eml_meta["doc_id"])
    assert "发件人：sender@example.com" in eml_text
    assert "邮件正文内容" in eml_text

    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:项目会议\r\n"
        "DTSTART;TZID=Asia/Shanghai:20260820T093000\r\n"
        "DESCRIPTION:讨论\\,预算与排期\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode("utf-8")
    ics_meta = office_docs.create_session(user, "calendar.ics", ics)
    ics_text = office_docs.extract_full_text(user, ics_meta["doc_id"])
    assert "事件1：项目会议" in ics_text
    assert "开始：2026-08-20 09:30" in ics_text
    assert "说明：讨论,预算与排期" in ics_text


def test_extract_doc_text_rtf_masquerade(tmp_path):
    f = tmp_path / "fake.doc"
    f.write_bytes(b"{\\rtf1\\ansi Hello \\b World\\b0 }")
    text = office_docs._extract_doc_text(f)
    assert "Hello" in text


def test_extract_doc_text_plain(tmp_path):
    f = tmp_path / "plain.doc"
    f.write_bytes("纯文本内容 DOCCHAIN1".encode("utf-8"))
    text = office_docs._extract_doc_text(f)
    assert "DOCCHAIN1" in text


def test_apply_edits_text_ops(monkeypatch, tmp_path):
    monkeypatch.setattr(office_docs, "OFFICE_DIR", tmp_path / "office")
    user = "u1"
    meta = office_docs.create_session(user, "a.txt", b"hello world\nsecond line")
    doc_id = meta["doc_id"]
    records = office_docs.apply_edits(
        user, doc_id, [{"op": "search_replace", "old": "world", "new": "lumi", "first_only": True}]
    )
    assert any("已替换" in r for r in records)
    loaded = office_docs.load_session(user, doc_id)
    buffered = office_docs._buffered_path(Path(loaded["_session"]), ".txt")
    assert "hello lumi" in buffered.read_text(encoding="utf-8")

    # 空操作 → 如实报告未修改
    records2 = office_docs.apply_edits(user, doc_id, [])
    assert any("未生成任何可执行的编辑操作" in r for r in records2)


def test_revert_edits_discards_buffer(monkeypatch, tmp_path):
    monkeypatch.setattr(office_docs, "OFFICE_DIR", tmp_path / "office")
    user = "u1"
    meta = office_docs.create_session(user, "a.txt", b"original")
    doc_id = meta["doc_id"]
    office_docs.apply_edits(user, doc_id, [{"op": "rewrite", "content": "changed"}])
    office_docs.revert_edits(user, doc_id)
    meta2 = office_docs.load_session(user, doc_id)
    assert not office_docs._buffered_path(Path(meta2["_session"]), ".txt").exists()


def test_pptx_delete_and_add_slide(tmp_path):
    from pptx import Presentation

    path = tmp_path / "t.pptx"
    prs = Presentation()
    for t in ["一", "二", "三"]:
        s = prs.slides.add_slide(prs.slide_layouts[0])
        s.shapes.title.text = t
    prs.save(str(path))

    r = office_docs._apply_pptx_op(path, {"op": "delete_slide", "slide_index": 1})
    assert "已删除第1页" in r
    prs2 = Presentation(str(path))
    assert [s.shapes.title.text for s in prs2.slides] == ["一", "三"]

    r2 = office_docs._apply_pptx_op(path, {"op": "add_slide", "title": "新页", "text": "内容"})
    assert "已在末尾新增一页" in r2
    prs3 = Presentation(str(path))
    assert len(prs3.slides) == 3
    assert prs3.slides[-1].shapes.title.text == "新页"

    with pytest.raises(ValueError):
        office_docs._apply_pptx_op(path, {"op": "delete_slide", "slide_index": 9})


def test_office_script_agent_passes_doc_ids(monkeypatch):
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.office.agents import OfficeScriptAgent

    agent = OfficeScriptAgent()
    captured = {}

    async def fake_generate(task, names, ctx):
        return "print('ok')"

    async def fake_run_skill(skill, params, ctx):
        captured["skill"] = skill
        captured["params"] = params
        return {
            "success": True,
            "content": "运行成功",
            "outputs": [{"name": "out.csv", "size": 10, "doc_id": "d1"}],
        }

    monkeypatch.setattr(agent, "_generate_script", fake_generate)
    monkeypatch.setattr(agent, "run_skill", fake_run_skill)
    node = TaskNode(
        id="n1", name="脚本", agent="office_script",
        params={"task": "导出csv", "doc_ids": ["d1", "d2"]}, depends_on=[],
    )
    res = asyncio.run(agent.execute(node, WorkerContext(user_id="u1", job_id="j1")))
    assert res["success"] is True
    assert captured["skill"] == "python_exec"
    assert captured["params"]["doc_ids"] == ["d1", "d2"]
    assert res["outputs"][0]["name"] == "out.csv"


def test_analyze_doc_limits_direct_prompt_and_output(monkeypatch):
    captured = {}

    async def fake_ensure(*args, **kwargs):
        return {}

    async def fake_compute(fn, *args):
        return "x" * 12_001

    async def fake_index(*args, **kwargs):
        return {}

    class DummySession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    async def fake_search(*args, **kwargs):
        return "y" * 30_000, []

    async def fake_llm(context, system, user, **kwargs):
        captured["user"] = user
        captured["max_tokens"] = kwargs["max_tokens"]
        return "answer"

    monkeypatch.setattr(office_docs, "ensure_session", fake_ensure)
    monkeypatch.setattr("app.core.executors.run_in_compute", fake_compute)
    monkeypatch.setattr(office_docs, "ensure_rag_index", fake_index)
    monkeypatch.setattr("app.core.database.async_session_factory", lambda: DummySession())
    monkeypatch.setattr("app.services.rag.knowledge.search_user_knowledge", fake_search)
    monkeypatch.setattr("app.services.office_skill_utils.office_llm", fake_llm)

    result = asyncio.run(office_docs.analyze_doc("u1", "d1", "总结"))

    assert result["answer"] == "answer"
    assert captured["max_tokens"] == 1800
    assert len(captured["user"].split("文档片段：\n", 1)[1]) == 24_000


def test_office_script_generation_uses_one_llm_call(monkeypatch):
    from app.agents.core.base import WorkerContext
    from app.agents.roles.office.agents import OfficeScriptAgent

    calls = []

    class FakeLLM:
        async def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return "print('ok')"

    monkeypatch.setattr("app.core.llm.LLMClient", FakeLLM)

    code = asyncio.run(
        OfficeScriptAgent()._generate_script(
            "把成绩表导出为摘要 CSV", ["scores.csv"], WorkerContext(user_id="u1", job_id="j1")
        )
    )

    assert code == "print('ok')"
    assert len(calls) == 1
    assert calls[0][1]["max_tokens"] == 4000
