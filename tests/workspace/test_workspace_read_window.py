"""统一工作区读取（原“受限只读读取窗口”）的回归测试。

只验证入口决策与统一读取契约；LLM 一律用 stub，不发起真实调用。
"""

from __future__ import annotations

import asyncio

from app.services.orchestrator import Orchestrator, _workspace_content_question


class _FakeLLM:
    provider = "fake"

    async def start(self) -> None:  # pragma: no cover - no-op
        return

    def __init__(self, content: str = "", calls=None, final: str = "最终总结"):
        self._content = content
        self._calls = calls or []
        self._final = final

    async def chat_with_tools(self, _messages, _tools, **_kwargs):
        if self._calls:
            emitted = list(self._calls)
            self._calls = []
            return self._content, emitted
        return self._content, []

    async def chat(self, _messages, **_kwargs):
        return self._final


def test_workspace_content_question_heuristic():
    assert _workspace_content_question("README 里讲了什么")
    assert _workspace_content_question("看一下 src 下面的代码结构")
    assert _workspace_content_question("这个文件的内容是什么")
    assert not _workspace_content_question("帮我写一封请假邮件")
    assert not _workspace_content_question("什么是光合作用")


def _service(fake_llm) -> Orchestrator:
    service = Orchestrator()
    service._llm = fake_llm
    service._llm_started = True
    return service


def _ready_summary() -> str:
    return (
        "当前对话绑定了一个本地工作区「项目」（workspace_id=ws1）。\n"
        "工作区已注册且可访问（当前版本 v3）。\n"
        "根目录包含：\n- README.md（文件）\n"
        "当前没有暂存修改。\n可用读取能力：workspace_read、workspace_search。"
    )


def test_bounded_read_returns_none_when_reader_has_no_content(monkeypatch):
    """统一读取服务无正文（未注册/离线/无命中）时，旧安全路径必须回退。"""
    from app.workspace.read import reader as wr

    async def fake_read(_self, _request, **_kwargs):
        return {
            "status": "failed",
            "summary": "工作区托管设备当前离线；暂时无法读取目录或文件。",
            "content": [],
            "has_more": False,
            "cursor": None,
            "meta": {"error_code": "WORKSPACE_DEVICE_OFFLINE"},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service = _service(_FakeLLM())
    text, records = asyncio.run(service._bounded_workspace_read(
        user_id="u1", user_role="user", conversation_id="c1",
        content="README 里讲了什么", workspace_id="ws1",
        workspace_summary="工作区：设备离线，无法读取。", llm_api_key=None,
    ))
    assert text is None
    assert records and records[0]["status"] == "failed"


def test_bounded_read_disabled_without_content_intent(monkeypatch):
    async def caps(_user, _scene, _role, _ws):
        return [type("C", (), {"name": "mcp__lumi_pc__workspace_read",
                               "description": "", "parameters": {"type": "object", "properties": {}}})()]
    monkeypatch.setattr("app.agents.skills.executor.get_workspace_navigator_capability", caps)
    service = _service(_FakeLLM())
    text, _records = asyncio.run(service._bounded_workspace_read(
        user_id="u1", user_role="user", conversation_id="c1",
        content="帮我写一封请假邮件", workspace_id="ws1",
        workspace_summary=_ready_summary(), llm_api_key=None,
    ))
    assert text is None


def test_bounded_read_without_content_falls_back_to_summary_path():
    """统一读取没有正文时返回 None，由调用方走“基于摘要直答”的旧路径。"""
    service = _service(_FakeLLM(content="基于摘要直接回答。", calls=[], final="基于摘要直接回答。"))
    text, records = asyncio.run(service._bounded_workspace_read(
        user_id="u1", user_role="user", conversation_id="c1",
        content="README 里讲了什么", workspace_id="ws1",
        workspace_summary=_ready_summary(), llm_api_key=None,
    ))
    assert text is None
    assert records and records[0]["tool"] == "workspace_navigator"


def test_bounded_read_uses_unified_reader_and_injects_structured_text(monkeypatch):
    """旧安全路径改为统一读取：读取一次结构化正文 → 模型收敛回答。"""
    from app.workspace.read import reader as wr

    captured: dict = {}

    async def fake_read(_self, request, **_kwargs):
        captured["request"] = request
        return {
            "status": "success",
            "summary": "已读取 README.md（1/1 段）",
            "content": [{
                "source": "README.md", "location": "line-1", "title": "项目说明",
                "text": "内容：项目说明。",
            }],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "markdown", "parser": "workspace_read", "workspace_version": 3},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service = _service(_FakeLLM(content="", calls=[], final="已读取：项目说明。"))
    text, records = asyncio.run(service._bounded_workspace_read(
        user_id="u1", user_role="user", conversation_id="c1",
        content="README 讲了什么", workspace_id="ws1",
        workspace_summary=_ready_summary(), llm_api_key=None,
    ))
    assert captured["request"] == "README 讲了什么"
    assert text == "已读取：项目说明。"
    assert records and records[0]["tool"] == "workspace_navigator"
    assert records[0]["source"] == "README.md"
    assert records[0]["location"] == "line-1"


def test_bounded_read_delivers_evidence_when_convergence_fails(monkeypatch):
    """收敛模型不可用时直接交付读取到的正文（不丢失资料、不谎报失败）。"""
    from app.workspace.read import reader as wr

    async def fake_read(_self, _request, **_kwargs):
        return {
            "status": "success",
            "summary": "已读取答辩.pptx",
            "content": [{
                "source": "答辩.pptx", "location": "slide-1", "title": "概述",
                "text": "PPT 正文",
            }],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "pptx", "parser": "workspace_content_extract"},
        }

    class BrokenLLM(_FakeLLM):
        async def chat(self, *args, **kwargs):
            raise RuntimeError("model down")

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service = _service(BrokenLLM())
    text, _records = asyncio.run(service._bounded_workspace_read(
        user_id="u1", user_role="user", conversation_id="c1",
        content="根据 PPT 回答", workspace_id="ws1",
        workspace_summary=_ready_summary(), llm_api_key=None,
    ))
    assert text and "PPT 正文" in text


def test_complete_document_request_consumes_workspace_cursor(monkeypatch):
    """“整份/全文”请求必须自动续读，不能停在首批页面。"""
    from app.workspace.read import reader as wr

    calls: list[dict] = []

    async def fake_read(_self, request, *, path="", cursor="", max_chars=12000):
        calls.append({"request": request, "cursor": cursor, "max_chars": max_chars})
        if not cursor:
            return {
                "status": "partial",
                "summary": "已读取答辩.pptx 的前半部分",
                "content": [{
                    "source": "答辩.pptx", "location": "slide-1", "title": "第1页",
                    "text": "第一页正文",
                }],
                "has_more": True,
                "cursor": "cursor-next",
                "meta": {"format": "pptx", "parser": "pptx", "pages_read": 1},
            }
        return {
            "status": "success",
            "summary": "已读取答辩.pptx 到结尾",
            "content": [{
                "source": "答辩.pptx", "location": "slide-40", "title": "第40页",
                "text": "最后一页正文",
            }],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "pptx", "parser": "pptx", "pages_read": 1},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service = _service(_FakeLLM(content="", calls=[], final="完整总结"))
    text, records = asyncio.run(service.read_workspace_context(
        user_id="u1", user_role="user", conversation_id="c1",
        content="请通读整份答辩 PPT 后总结", workspace_id="ws1",
        workspace_summary=_ready_summary(), llm_api_key=None,
    ))
    assert "第一页正文" in text and "最后一页正文" in text
    assert len(calls) == 2
    assert calls[1]["cursor"] == "cursor-next"
    assert records[0]["read_complete"] is True


def test_on_demand_document_request_keeps_cursor_for_follow_up(monkeypatch):
    """非完整请求仍保留按需分页，不为普通问题无条件读取整份文档。"""
    from app.workspace.read import reader as wr

    calls: list[dict] = []

    async def fake_read(_self, request, *, path="", cursor="", max_chars=12000):
        calls.append({"request": request, "cursor": cursor})
        return {
            "status": "partial",
            "summary": "已读取相关片段",
            "content": [{
                "source": "答辩.pptx", "location": "slide-1", "title": "相关页",
                "text": "与问题相关的正文",
            }],
            "has_more": True,
            "cursor": "cursor-next",
            "meta": {"format": "pptx", "parser": "pptx", "pages_read": 1},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service = _service(_FakeLLM(content="", calls=[], final="按需回答"))
    text, records = asyncio.run(service.read_workspace_context(
        user_id="u1", user_role="user", conversation_id="c1",
        content="答辩 PPT 中系统架构是什么？", workspace_id="ws1",
        workspace_summary=_ready_summary(), llm_api_key=None,
    ))
    assert "与问题相关的正文" in text
    assert len(calls) == 1
    assert records[0]["read_complete"] is False
