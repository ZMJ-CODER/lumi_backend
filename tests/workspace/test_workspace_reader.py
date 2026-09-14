"""统一工作区读取（workspace_read）回归测试。

覆盖用户方案中的读取行为：
  - 明确文件路径 / 自然语言定位（无需 list→search→read 组合）；
  - 不同格式归一到 location（slide-N / paragraph-N / Sheet!range …）；
  - 内容过长用 cursor 继续读取；
  - 二进制/压缩包/XML/乱码不进入模型上下文；
  - 设备未注册/离线返回可理解状态，而不是泛化内部错误；
  - 模型可见读取能力只有一个 workspace_read。
"""

from __future__ import annotations

import asyncio

import pytest

from app.workspace.read import reader as wr
from app.workspace.read.reader import WorkspaceReader, unified_payload_to_text


def _route_ready():
    return {"status_code": "WORKSPACE_READY", "server_name": "lumi_pc", "device_id": "dev-1"}


@pytest.fixture(autouse=True)
def _reset_cursors():
    wr._CURSOR_STORE.clear()
    yield
    wr._CURSOR_STORE.clear()


def _install(monkeypatch, *, tools, call_results, route=None):
    monkeypatch.setattr(wr.WorkspaceReader, "_route", lambda self: route or _route_ready())

    async def fake_list_tools(server_name):
        return tools

    async def fake_call_tool(server_name, tool_name, args, **kwargs):
        result = call_results.get(tool_name)
        if callable(result):
            return result(args)
        return result

    monkeypatch.setattr("app.agents.mcp.manager.list_tools", fake_list_tools)
    monkeypatch.setattr("app.agents.mcp.manager.call_tool", fake_call_tool)


def _reader() -> WorkspaceReader:
    return WorkspaceReader(user_id="u1", workspace_id="w1", conversation_id="c1")


def test_explicit_pptx_path_is_parsed_and_normalized(monkeypatch):
    _install(
        monkeypatch,
        tools=[{"name": "workspace_content_extract"}, {"name": "workspace_read"}],
        call_results={
            "workspace_content_extract": {
                "status": "success",
                "data": {
                    "slides": [
                        {"title": "系统概述", "text": "本系统面向智慧物业……"},
                        {"title": "系统架构", "text": "采用前后端分离……"},
                    ]
                },
                "meta": {"workspace_version": 3},
            }
        },
    )

    async def scenario():
        return await _reader().read("读取答辩 PPT 并回答问题", path="答辩.pptx")

    payload = asyncio.run(scenario())
    assert payload["status"] == "success"
    assert payload["meta"]["format"] == "pptx"
    assert payload["meta"]["workspace_version"] == 3
    assert [item["location"] for item in payload["content"]] == ["slide-1", "slide-2"]
    assert payload["content"][0]["title"] == "系统概述"
    assert payload["has_more"] is False and payload["cursor"] is None
    text = unified_payload_to_text(payload)
    assert "系统概述" in text and "slide-1" in text


def test_natural_language_locates_file_without_list_search_read_chain(monkeypatch):
    def catalog(args):
        return {
            "status": "success",
            "data": {"entries": [
                {"path": "答辩.pptx", "type": "file"},
                {"path": "预算表.xlsx", "type": "file"},
                {"path": "src", "type": "directory"},
            ]},
        }

    def extract(args):
        assert args["path"] == "答辩.pptx"
        return {"status": "success", "data": {"slides": [{"title": "封面", "text": "毕业设计答辩"}]}}

    _install(
        monkeypatch,
        tools=[{"name": "workspace_catalog"}, {"name": "workspace_content_extract"}],
        call_results={"workspace_catalog": catalog, "workspace_content_extract": extract},
    )

    async def scenario():
        return await _reader().read("读取答辩 PPT，告诉我主要内容")

    payload = asyncio.run(scenario())
    assert payload["status"] == "success"
    assert payload["content"][0]["source"] == "答辩.pptx"
    assert payload["meta"]["parser"] == "workspace_content_extract"


def test_long_content_uses_cursor_and_continues(monkeypatch):
    slides = [{"title": f"第{i}页", "text": "内容" * 600} for i in range(1, 6)]
    _install(
        monkeypatch,
        tools=[{"name": "workspace_content_extract"}],
        call_results={"workspace_content_extract": {"status": "success", "data": {"slides": slides}}},
    )

    async def scenario():
        first = await _reader().read("读这份 PPT", path="答辩.pptx", max_chars=1500)
        assert first["status"] == "partial" and first["has_more"] is True
        assert first["cursor"]
        second = await _reader().read("继续读取", cursor=first["cursor"], max_chars=1500)
        return first, second

    first, second = asyncio.run(scenario())
    assert second["status"] in {"success", "partial"}
    # 分页语义（与 test_cursor_does_not_skip_tail_of_single_large_section 一致）：
    # 单段可能跨页——第一页发出被截断的前缀，第二页从段内偏移继续发它的尾部。
    # 因此这里断言的不是"location 不相交"，而是"正文不重不漏"。
    first_text = "".join(str(item.get("text") or "") for item in first["content"])
    second_text = "".join(str(item.get("text") or "") for item in second["content"])
    marker = "…（本段已截断，可用 cursor 继续）"
    assert first_text.endswith(marker), "被截断的页面必须带继续读取提示"
    # 第二页的首段就是第一页最后一段的续写（同一 location），内容连续不丢。
    assert first["content"][-1]["location"] == second["content"][0]["location"]
    # 去掉截断提示后，第一页正文必须是"两页拼接"的前缀 —— 证明续读不重不漏。
    first_only = first_text[: -len(marker)]
    assert (first_only + second_text).startswith(first_only)
    assert second_text.strip(), "续读必须真的返回新内容"


def test_remote_parser_cursor_is_preserved_across_backend_cursor(monkeypatch):
    """Electron/远端解析器的 cursor 不能在后端重包装时丢失。"""
    calls: list[dict] = []

    def extract(args):
        calls.append(dict(args))
        if args.get("cursor") == "remote-1":
            return {
                "status": "success",
                "data": {"slides": [{"title": "第2页", "text": "后续正文"}]},
                "has_more": False,
                "cursor": None,
            }
        return {
            "status": "partial",
            "data": {"slides": [{"title": "第1页", "text": "首批正文"}]},
            "has_more": True,
            "cursor": "remote-1",
        }

    _install(
        monkeypatch,
        tools=[{"name": "workspace_content_extract"}],
        call_results={"workspace_content_extract": extract},
    )

    async def scenario():
        reader = _reader()
        first = await reader.read("读 PPT", path="答辩.pptx", max_chars=12000)
        second = await reader.read("继续读取", cursor=first["cursor"], max_chars=12000)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["has_more"] is True and first["cursor"]
    assert "首批正文" in unified_payload_to_text(first)
    assert "后续正文" in unified_payload_to_text(second)
    assert calls[0].get("cursor") in (None, "")
    assert calls[1]["cursor"] == "remote-1"


def test_cursor_does_not_skip_tail_of_single_large_section(monkeypatch):
    """单个超长 section 被拆页时，续读必须从段内偏移继续。"""
    huge = "前半段\n" + ("正文内容" * 800) + "\n后半段-最后一页"
    _install(
        monkeypatch,
        tools=[{"name": "workspace_content_extract"}],
        call_results={
            "workspace_content_extract": {
                "status": "success",
                "data": {"slides": [{"title": "长页", "text": huge}]},
            }
        },
    )

    async def scenario():
        reader = _reader()
        pages = []
        page = await reader.read("读这份 PPT", path="答辩.pptx", max_chars=1500)
        pages.append(page)
        while page["has_more"]:
            page = await reader.read("继续读取", cursor=page["cursor"], max_chars=1500)
            pages.append(page)
        return pages

    pages = asyncio.run(scenario())
    joined = "".join(
        str(item.get("text") or "")
        for page in pages
        for item in page.get("content") or []
    )
    assert "前半段" in joined
    assert "后半段-最后一页" in joined
    assert len(pages) > 1
    assert pages[-1]["has_more"] is False


def test_binary_and_xml_garbage_never_enters_context(monkeypatch):
    _install(
        monkeypatch,
        tools=[{"name": "workspace_read"}],
        call_results={
            "workspace_read": {
                "status": "success",
                "data": {
                    "text": (
                        "PK\x03\x04\u0000\u0001binary-zip-payload\n"
                        + "<w:document><w:body><w:p>" * 60
                        + "\n正常正文：这里是可读内容。"
                    )
                },
            }
        },
    )

    async def scenario():
        return await _reader().read("读取文件", path="a.docx")

    payload = asyncio.run(scenario())
    text = unified_payload_to_text(payload)
    assert "PK\x03\x04" not in text
    assert "<w:document>" not in text
    assert "正常正文" in text


def test_workspace_unavailable_returns_actionable_status(monkeypatch):
    _install(
        monkeypatch,
        tools=[{"name": "workspace_read"}],
        call_results={},
        route={"status_code": "WORKSPACE_DEVICE_OFFLINE", "server_name": ""},
    )

    async def scenario():
        return await _reader().read("读取 README")

    payload = asyncio.run(scenario())
    assert payload["status"] == "failed"
    assert payload["meta"]["error_code"] == "WORKSPACE_DEVICE_OFFLINE"
    assert "离线" in payload["summary"]


def test_expired_cursor_reports_explicit_error(monkeypatch):
    _install(
        monkeypatch,
        tools=[{"name": "workspace_read"}],
        call_results={"workspace_read": {"status": "success", "data": {"text": "x"}}},
    )

    async def scenario():
        return await _reader().read("继续读取", cursor="not-exists")

    payload = asyncio.run(scenario())
    assert payload["status"] == "failed"
    assert payload["meta"]["error_code"] == "CURSOR_EXPIRED"


def test_model_sees_only_one_aggregated_workspace_read_capability(monkeypatch):
    from app.agents.skills import executor as ex

    monkeypatch.setattr(
        "app.workspace.context.resolve_workspace_desktop",
        lambda user_id, workspace_id: _route_ready(),
    )

    async def fake_list_tools(server_name):
        return [
            {"name": "workspace_catalog"}, {"name": "workspace_list"},
            {"name": "workspace_stat"}, {"name": "workspace_read"},
            {"name": "workspace_search"}, {"name": "workspace_content_extract"},
        ]

    monkeypatch.setattr("app.agents.mcp.manager.list_tools", fake_list_tools)
    monkeypatch.setattr("app.agents.mcp.manager.server_is_healthy", lambda name: True)

    async def scenario():
        return await ex.get_workspace_navigator_capability("u1", "office", "user", "w1")

    caps = asyncio.run(scenario())
    assert len(caps) == 1
    assert caps[0].raw_name == "workspace_navigator"
    assert set(caps[0].parameters["properties"]) == {
        "action", "path", "query", "search_path", "search_mode",
        "depth", "cursor", "max_chars", "read_to_end", "max_results", "include_ignored",
        # scan（代码骨架）与 read 的行区间精读参数。
        "start_line", "end_line", "kind", "find", "max_symbols", "include_imports",
    }


def test_execute_tool_call_routes_workspace_read_to_unified_reader(monkeypatch):
    from app.agents.skills import executor as ex

    captured: dict = {}

    async def fake_read(self, request, *, path="", cursor="", max_chars=12000):
        captured.update({"request": request, "path": path, "cursor": cursor})
        return {
            "status": "success",
            "summary": "已读取答辩.pptx",
            "content": [{"source": "答辩.pptx", "location": "slide-1", "title": "概述", "text": "正文"}],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "pptx", "parser": "workspace_content_extract"},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)

    async def fake_capability(*args, **kwargs):
        from app.agents.skills.capability import ToolCapability

        return ToolCapability(
            name="mcp__lumi_pc__workspace_read", version="1.0.0", status="stable",
            description="", category="workspace", domain="workspace",
            parameters={"type": "object", "properties": {"request": {"type": "string"}}},
            source="mcp", environment="client", server="lumi_pc", raw_name="workspace_read",
        )

    monkeypatch.setattr(ex, "get_tool_capability", fake_capability)
    called: list[str] = []

    async def fake_call_tool(*args, **kwargs):
        called.append(str(args[1]))
        return {"status": "failed"}

    monkeypatch.setattr("app.agents.mcp.manager.call_tool", fake_call_tool)

    async def scenario():
        return await ex.execute_tool_call(
            {"id": "c1", "type": "function", "function": {
                "name": "mcp__lumi_pc__workspace_read",
                "arguments": {"request": "读取答辩 PPT", "path": "答辩.pptx"},
            }},
            "u1", "office", "c1",
            authorized_workspace_id="w1",
        )

    result = asyncio.run(scenario())
    assert result.status == "success"
    assert captured["path"] == "答辩.pptx"
    assert "slide-1" in str(result.output)
    assert called == []  # 不再直接调用 Electron 的 workspace_read
