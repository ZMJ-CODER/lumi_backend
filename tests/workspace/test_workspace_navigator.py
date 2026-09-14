"""``workspace_navigator`` 聚合读取域回归测试（后端侧）。

覆盖产品方案中后端必须保证的行为：

* 只有一个模型可见入口，内部原子读取名不再出现在 function calling schema；
* 统一信封（status/action/summary/data/has_more/cursor/meta/error）与稳定错误码；
* read 原子性：目录 / 批量 / 缺参一律拒绝，并给出可自我修正的建议；
* 参数钳制（depth / max_results / max_chars）与游标分页、过期游标；
* workspace_id 由服务端注入，模型传入值不构成授权依据。
"""

from __future__ import annotations

import asyncio

import pytest

from app.workspace.read import navigator as wn
from app.workspace.read.navigator import WorkspaceNavigatorService

READY = {"status_code": "WORKSPACE_READY", "server_name": "lumi_pc", "device_id": "dev-1"}
ADVERTISED = (
    {"name": "workspace_list"},
    {"name": "workspace_search"},
    {"name": "workspace_content_extract"},
    {"name": "workspace_read"},
)


@pytest.fixture(autouse=True)
def _reset_cursors():
    wn.CURSOR_STORE.clear()
    yield
    wn.CURSOR_STORE.clear()


def _service(results=None, *, route=None, advertised=ADVERTISED, **kwargs):
    calls: list[tuple[str, dict]] = []

    async def fake_list_tools(server_name):
        return list(advertised)

    async def fake_call_tool(server_name, tool_name, args):
        calls.append((tool_name, dict(args)))
        value = (results or {}).get(tool_name)
        if callable(value):
            return value(args)
        return value if value is not None else {"status": "success", "data": {}}

    service = WorkspaceNavigatorService(
        user_id="u1",
        workspace_id="w1",
        conversation_id="c1",
        resolve_route=lambda: dict(route or READY),
        list_tools=fake_list_tools,
        call_tool=fake_call_tool,
        **kwargs,
    )
    return service, calls


def _run(service, action, params=None):
    return asyncio.run(service.execute(action, params or {}))


# ── 信封与参数校验 ──────────────────────────────────────────

def test_invalid_action_returns_stable_error_without_calls():
    service, calls = _service()
    payload = _run(service, "delete")
    assert payload["status"] == "error"
    assert payload["error"]["code"] == wn.INVALID_ACTION
    assert payload["error"]["suggested_action"]
    assert calls == []
    assert set(payload) == {"status", "action", "summary", "data", "has_more", "cursor", "meta", "error"}


def test_unbound_workspace_reports_not_bound():
    service, _calls = _service(route={"status_code": "WORKSPACE_NOT_BOUND", "server_name": ""})
    payload = _run(service, "list")
    assert payload["status"] == "error"
    assert payload["error"]["code"] == wn.WORKSPACE_NOT_BOUND
    assert payload["has_more"] is False and payload["cursor"] is None


def test_offline_device_reports_device_offline():
    service, _calls = _service(route={"status_code": "WORKSPACE_DEVICE_OFFLINE", "server_name": ""})
    payload = _run(service, "read", {"path": "a.txt"})
    assert payload["error"]["code"] == wn.WORKSPACE_DEVICE_OFFLINE
    assert payload["status"] == "error"


def test_search_requires_query_and_read_requires_single_path():
    service, calls = _service()
    missing_query = _run(service, "search", {})
    assert missing_query["error"]["code"] == wn.INVALID_PARAMS
    missing_path = _run(service, "read", {})
    assert missing_path["error"]["code"] == wn.INVALID_PARAMS
    assert calls == []


def test_model_supplied_workspace_id_is_ignored():
    service, calls = _service(
        results={"workspace_list": {"status": "success", "data": {"entries": []}}}
    )
    _run(service, "list", {"workspace_id": "attacker-workspace"})
    assert calls and calls[0][1]["workspace_id"] == "w1"


def test_path_escape_is_rejected():
    service, calls = _service()
    payload = _run(service, "list", {"path": "../../etc"})
    assert payload["error"]["code"] == wn.INVALID_PARAMS
    assert calls == []


# ── list ───────────────────────────────────────────────────

def _entry(path: str, *, kind: str = "file", size: int = 10, ignored: bool = False) -> dict:
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "type": kind,
        "size": size,
        "mtime": 1700000000,
        "ignored": ignored,
    }


def _paged(items: list[dict], *, cursor: str, has_more: bool, meta: dict | None = None) -> dict:
    """模拟 Electron 原子工具的分页信封（自包含 nav1 cursor）。"""
    return {
        "status": "success",
        "data": {"entries": items, "meta": meta or {}},
        "has_more": has_more,
        "cursor": cursor,
    }


# 两页目录：首页带 nav1 cursor，续页用尽。
def _list_two_pages(args):
    if args.get("cursor") == "nav1-page2":
        return _paged([_entry("src/f3.txt"), _entry("src", kind="directory")], cursor="", has_more=False)
    return _paged(
        [_entry("src/f1.txt"), _entry("src/f2.txt")],
        cursor="nav1-page2",
        has_more=True,
        meta={"filtered_ignored": 4},
    )


def test_list_returns_metadata_only_and_hosts_electron_cursor():
    service, calls = _service(results={"workspace_list": _list_two_pages})
    first = _run(service, "list", {"path": "src", "depth": 99, "max_results": 10})
    assert first["status"] == "partial" and first["has_more"] is True
    assert first["action"] == "list"
    assert first["meta"]["depth"] == wn.MAX_DEPTH  # depth 被钳制
    assert first["meta"]["filtered_ignored"] == 4
    assert first["meta"]["include_ignored"] is False
    entry = first["data"]["entries"][0]
    assert set(entry) == {"name", "path", "kind", "size", "modified_at", "ext", "ignored"}
    assert entry["kind"] == "file" and entry["ext"] == ".txt"
    assert "text" not in entry and "content" not in entry
    # 首页向 Electron 要了 max_entries，且不带 cursor。
    tool, args = calls[0]
    assert tool == "workspace_list"
    assert args["path"] == "src" and args["depth"] == wn.MAX_DEPTH
    assert args["max_entries"] >= 10 and "cursor" not in args

    # 续页：后端托管 Electron 的 nav1 cursor 并原样回传（不重编码）。
    second = _run(service, "list", {"cursor": first["cursor"]})
    assert second["has_more"] is False
    assert [item["path"] for item in second["data"]["entries"]] == ["src/f3.txt", "src"]
    assert calls[1][1]["cursor"] == "nav1-page2"
    assert calls[1][1]["include_ignored"] is False


def test_list_forwards_include_ignored_and_binds_it_to_cursor(monkeypatch):
    captured: list[dict] = []

    def fetch(args):
        captured.append(dict(args))
        if args.get("cursor"):
            return _paged([_entry(".env", ignored=True)], cursor="", has_more=False)
        return _paged([_entry("node_modules", kind="directory", ignored=True)], cursor="nav1-ig", has_more=True)

    service, _calls = _service(results={"workspace_list": fetch})
    first = _run(service, "list", {"include_ignored": True})
    assert first["meta"]["include_ignored"] is True
    assert first["data"]["entries"][0]["ignored"] is True
    assert captured[0]["include_ignored"] is True
    # 续页即使用户不再传 include_ignored，也沿用首次绑定值（避免 Electron CURSOR_EXPIRED）。
    service2, calls2 = _service(results={"workspace_list": fetch})
    first2 = _run(service2, "list", {"include_ignored": True})
    _run(service2, "list", {"cursor": first2["cursor"]})
    assert captured[1]["include_ignored"] is True


def test_list_falls_back_to_backend_offset_when_electron_has_no_cursor():
    """Electron 不分页（无 cursor）时，后端仍能按 max_results 切片。"""
    entries = [_entry(f"f{i}.txt") for i in range(5)]
    service, _calls = _service(
        results={"workspace_list": {"status": "success", "data": {"entries": entries}, "has_more": False}}
    )
    first = _run(service, "list", {"max_results": 2})
    assert first["has_more"] is True and len(first["data"]["entries"]) == 2
    second = _run(service, "list", {"cursor": first["cursor"]})
    assert [item["path"] for item in second["data"]["entries"]] == ["f2.txt", "f3.txt"]
    third = _run(service, "list", {"cursor": second["cursor"]})
    assert len(third["data"]["entries"]) == 1 and third["has_more"] is False


def test_list_uses_root_when_path_missing():
    service, calls = _service(results={"workspace_list": _paged([_entry("a.txt")], cursor="", has_more=False)})
    payload = _run(service, "list", {})
    assert payload["status"] == "ok"
    assert calls[0][1]["path"] == ""
    assert payload["meta"]["path"] == "."


def test_list_empty_directory_is_not_an_error():
    service, _calls = _service(
        results={"workspace_list": _paged([], cursor="", has_more=False, meta={"filtered_ignored": 2})}
    )
    payload = _run(service, "list", {})
    assert payload["status"] == "empty"
    assert payload["error"] is None
    assert payload["meta"]["filtered_ignored"] == 2


def test_list_max_results_and_max_entries_are_clamped():
    service, calls = _service(results={"workspace_list": _paged([_entry("a.txt")], cursor="", has_more=False)})
    payload = _run(service, "list", {"max_results": 100000})
    assert len(payload["data"]["entries"]) <= wn.MAX_MAX_RESULTS
    assert calls[0][1]["depth"] == wn.DEFAULT_DEPTH
    assert calls[0][1]["max_entries"] <= wn.MAX_ELECTRON_LIST_ENTRIES


# ── search ─────────────────────────────────────────────────

def test_search_normalizes_matches_and_never_returns_full_document():
    long_body = "正文" * 5000
    service, calls = _service(results={
        "workspace_search": {
            "status": "success",
            "data": {
                "matches": [{
                    "path": "src/auth_service.py",
                    "page": 3,
                    "line": 42,
                    "sheet": "Sheet1",
                    "text": long_body,
                    "match_type": "content",
                }, "docs/登录说明.docx"],
            },
            "has_more": False,
            "cursor": "",
        }
    })
    payload = _run(service, "search", {"query": "登录|认证|token", "search_path": "src"})
    assert payload["status"] == "ok" and payload["action"] == "search"
    assert payload["meta"]["search_mode"] == "auto"
    first = payload["data"]["matches"][0]
    assert first["path"] == "src/auth_service.py"
    assert first["line"] == 42 and first["page"] == 3
    assert len(first["context"]) <= wn.CONTEXT_SNIPPET
    assert first["format"] == "text"
    assert first["sensitive"] is False
    assert len(payload["data"]["matches"]) == 2
    assert calls[0][0] == "workspace_search"
    assert calls[0][1]["search_path"] == "src"
    # query 原样透传（Electron 侧把 | 当分隔符做 OR 命中），后端不拆词。
    assert calls[0][1]["query"] == "登录|认证|token"
    # auto 不显式下发 search_mode，交给 Electron 组合检索。
    assert "search_mode" not in calls[0][1]


def test_search_marks_sensitive_matches():
    service, _calls = _service(results={
        "workspace_search": {
            "status": "success",
            "data": {"matches": [{"path": "config/.env", "line": 3, "text": "TOKEN=..."}]},
            "has_more": False,
            "cursor": "",
        }
    })
    payload = _run(service, "search", {"query": "TOKEN"})
    assert payload["data"]["matches"][0]["sensitive"] is True
    assert payload["meta"]["sensitive"] is True
    assert "凭据" in payload["summary"]


def test_search_passes_search_mode_verbatim():
    service, calls = _service(results={
        "workspace_search": {"status": "success", "data": {"matches": []}, "has_more": False, "cursor": ""}
    })
    _run(service, "search", {"query": "x", "search_mode": "content"})
    assert calls[0][1]["search_mode"] == "content"


def test_search_timeout_is_reported_with_stable_code():
    async def slow(server_name, tool_name, args):
        await asyncio.sleep(0.05)
        return {"status": "success", "data": {}}

    async def list_tools(server_name):
        return list(ADVERTISED)

    service = WorkspaceNavigatorService(
        user_id="u1",
        workspace_id="w1",
        resolve_route=lambda: dict(READY),
        list_tools=list_tools,
        call_tool=slow,
        timeout_s=1.0,
    )
    # 直接对内部原子调用做超时预算，避免依赖事件循环的调度抖动。
    service._timeout_s = 0.01
    payload = asyncio.run(service.execute("search", {"query": "x", "search_mode": "content"}))
    assert payload["status"] == "error"
    assert payload["error"]["code"] == wn.SEARCH_TIMEOUT


def test_search_hosts_electron_cursor_and_binds_mode():
    calls: list[dict] = []

    def search(args):
        calls.append(dict(args))
        if args.get("cursor") == "nav1-s2":
            return {
                "status": "success",
                "data": {"matches": [{"path": "b.md", "line": 9, "text": "y"}]},
                "has_more": False,
                "cursor": "",
            }
        return {
            "status": "success",
            "data": {"matches": [{"path": "a.md", "line": 1, "text": "x"}]},
            "has_more": True,
            "cursor": "nav1-s2",
        }

    service, _calls = _service(results={"workspace_search": search})
    first = _run(service, "search", {"query": "x", "search_mode": "content"})
    assert first["has_more"] is True and first["cursor"]
    assert calls[0]["search_mode"] == "content"

    second = _run(service, "search", {"cursor": first["cursor"]})
    assert [item["path"] for item in second["data"]["matches"]] == ["b.md"]
    assert second["has_more"] is False
    # 续页原样回传 Electron cursor，并沿用首次绑定的 search_mode。
    assert calls[1]["cursor"] == "nav1-s2"
    assert calls[1]["search_mode"] == "content"


# ── read ───────────────────────────────────────────────────

def test_read_directory_is_rejected_with_suggested_list():
    service, _calls = _service(results={
        "workspace_list": {
            "status": "success",
            "data": {"entries": [{"path": "docs", "type": "directory"}]},
        }
    })
    payload = _run(service, "read", {"path": "docs"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == wn.WORKSPACE_PATH_NOT_DIRECTORY
    assert payload["error"]["suggested_action"] == "list"


def test_read_directory_signal_from_client_is_normalized():
    service, _calls = _service(results={
        "workspace_list": {"status": "failed", "error_code": "EISDIR"},
    })
    payload = _run(service, "read", {"path": "docs"})
    assert payload["error"]["code"] == wn.WORKSPACE_PATH_NOT_DIRECTORY
    assert payload["error"]["suggested_action"] == "list"


def test_read_unsupported_binary_format_is_rejected_before_io():
    service, calls = _service()
    payload = _run(service, "read", {"path": "archive.zip"})
    assert payload["error"]["code"] == wn.WORKSPACE_UNSUPPORTED_FORMAT
    assert calls == []


def test_read_directory_with_known_extension_is_still_rejected():
    """目录名恰好带扩展名时也要靠探测拦住，不能把它当文件读。"""
    service, calls = _service(results={
        "workspace_list": {
            "status": "success",
            "data": {"entries": [{"path": "notes.md", "type": "directory"}]},
        }
    })
    payload = _run(service, "read", {"path": "notes.md"})
    assert payload["error"]["code"] == wn.WORKSPACE_PATH_NOT_DIRECTORY
    assert payload["error"]["suggested_action"] == "list"
    assert calls and calls[0][0] == "workspace_list"


def test_read_returns_sections_envelope_and_reuses_reader(monkeypatch):
    from app.workspace.read import reader as wr

    captured: dict = {}

    async def fake_read(self, request, *, path="", cursor="", max_chars=12000):
        captured.update({"path": path, "cursor": cursor, "max_chars": max_chars})
        return {
            "status": "partial",
            "summary": "已读取 答辩.pptx（1/2 段）",
            "content": [{
                "source": "答辩.pptx", "location": "slide-1",
                "title": "概述", "text": "系统由三层架构组成。",
            }],
            "has_more": True,
            "cursor": "inner-cursor",
            "meta": {"format": "pptx", "parser": "workspace_content_extract", "workspace_version": 7},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service, _calls = _service(results={
        "workspace_list": {"status": "success", "data": {"entries": [{"path": "答辩.pptx", "type": "file"}]}}
    })
    payload = _run(service, "read", {"path": "答辩.pptx", "max_chars": 1})
    assert payload["status"] == "partial"
    assert payload["action"] == "read"
    assert payload["data"]["path"] == "答辩.pptx"
    section = payload["data"]["sections"][0]
    assert section["location"] == "slide-1" and "三层架构" in section["text"]
    assert payload["has_more"] is True
    assert payload["meta"]["format"] == "pptx"
    assert captured["max_chars"] == wn.MIN_MAX_CHARS  # max_chars 被钳制到下限


def test_read_continuation_uses_cursor_path(monkeypatch):
    from app.workspace.read import reader as wr

    seen: list[tuple[str, str]] = []

    async def fake_read(self, request, *, path="", cursor="", max_chars=12000):
        seen.append((path, cursor))
        return {
            "status": "success",
            "summary": "续读完成",
            "content": [{"source": "a.txt", "location": "line-5", "title": "", "text": "后续内容"}],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "text"},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service, calls = _service()
    payload = _run(service, "read", {"path": "a.txt", "cursor": "v1-cursor"})
    # path 与 cursor 一起给（Electron 也允许仅 cursor 兜底，但契约上带上更稳）。
    assert seen == [("a.txt", "v1-cursor")]
    assert payload["meta"]["continued_from_cursor"] is True
    assert calls == []  # 续读不再做目录探测


def test_read_sensitive_file_is_redacted_and_capped(monkeypatch):
    from app.workspace.read import reader as wr

    captured: dict = {}

    async def fake_read(self, request, *, path="", cursor="", max_chars=12000):
        captured["max_chars"] = max_chars
        return {
            "status": "success",
            "summary": "已读取 .env",
            "content": [{
                "source": ".env", "location": "line-1", "title": "",
                "text": "DB_HOST=127.0.0.1\nDB_PASSWORD=hunter2xyz\nAPI_KEY=sk-abcdefghijklmnopqrst\n",
            }],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "text", "sensitive": True, "sensitivity": "CREDENTIAL"},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service, _calls = _service(results={
        "workspace_list": _paged([_entry("config/.env")], cursor="", has_more=False),
    })
    payload = _run(service, "read", {"path": "config/.env"})
    text = payload["data"]["sections"][0]["text"]
    # 普通/敏感读取都不弹确认，但单次上限固定为 4000（分页粒度，不是安全上限）。
    assert captured["max_chars"] == wn.READ_MAX_CHARS
    # 键名与非敏感值保留，凭据值被脱敏，原文不进入上下文。
    assert "DB_HOST=127.0.0.1" in text
    assert "hunter2xyz" not in text and "[REDACTED:CREDENTIAL]" in text
    assert "sk-abcdefghijklmnopqrst" not in text
    assert payload["meta"]["sensitive"] is True
    assert payload["meta"]["redacted"] is True
    assert payload["meta"]["redaction_count"] >= 2
    assert payload["meta"]["sensitivity"] == wn.SENSITIVITY_CREDENTIAL
    assert "脱敏" in payload["summary"]
    assert "hunter2xyz" not in wn.model_text(payload)


def test_read_pii_is_redacted_even_without_credential_filename(monkeypatch):
    from app.workspace.read import reader as wr

    async def fake_read(self, request, *, path="", cursor="", max_chars=12000):
        return {
            "status": "success",
            "summary": "已读取 通讯录.txt",
            "content": [{
                "source": "通讯录.txt", "location": "line-1", "title": "",
                "text": "联系人 张三 zhangsan@example.com 13800138000",
            }],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "text"},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service, _calls = _service(results={
        "workspace_list": _paged([_entry("通讯录.txt")], cursor="", has_more=False),
    })
    payload = _run(service, "read", {"path": "通讯录.txt"})
    text = payload["data"]["sections"][0]["text"]
    assert "zhangsan@example.com" not in text
    assert "13800138000" not in text
    assert payload["meta"]["redacted"] is True
    assert payload["meta"]["sensitivity"] == wn.SENSITIVITY_PII


def test_read_sensitive_continuation_is_redacted_too(monkeypatch):
    """敏感原文不能借 cursor 继续暴露：续页每一页都重新脱敏。"""
    from app.workspace.read import reader as wr

    async def fake_read(self, request, *, path="", cursor="", max_chars=12000):
        return {
            "status": "partial",
            "summary": "续读",
            "content": [{"source": ".env", "location": "line-9", "title": "", "text": "TOKEN=ghp_abcdefghijklmnopqrst\n"}],
            "has_more": True,
            "cursor": "v1-next",
            "meta": {"format": "text", "sensitive": True, "sensitivity": "CREDENTIAL"},
        }

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_read)
    service, _calls = _service()
    payload = _run(service, "read", {"path": "config/.env", "cursor": "v1-page1"})
    assert "ghp_abcdefghijklmnopqrst" not in payload["data"]["sections"][0]["text"]
    assert payload["meta"]["redacted"] is True


def test_expired_cursor_reports_cursor_expired():
    service, _calls = _service()
    payload = _run(service, "list", {"cursor": "not-a-cursor"})
    assert payload["error"]["code"] == wn.CURSOR_EXPIRED
    assert payload["has_more"] is False


def test_cursor_cannot_be_reused_across_actions():
    service, _calls = _service(results={
        "workspace_list": _paged(
            [_entry(f"f{i}.txt") for i in range(5)], cursor="nav1-x", has_more=True
        )
    })
    first = _run(service, "list", {"max_results": 1})
    assert first["cursor"]
    payload = _run(service, "search", {"query": "x", "cursor": first["cursor"]})
    assert payload["error"]["code"] == wn.CURSOR_EXPIRED


# ── 模型文本渲染 ───────────────────────────────────────────

def test_model_text_is_bounded_and_carries_error_guidance():
    payload = wn.build_error(
        wn.STATUS_ERROR, "read", wn.WORKSPACE_PATH_NOT_DIRECTORY, "docs 是目录",
        suggested_action="list",
    )
    text = wn.model_text(payload)
    assert "WORKSPACE_PATH_NOT_DIRECTORY" in text
    assert "list" in text

    sections = wn.normalize_sections([
        {"source": "a.txt", "location": "line-1", "title": "", "text": "x" * 9000}
    ])
    big = {
        "status": "partial", "action": "read", "summary": "长文", "data": {"sections": sections},
        "has_more": True, "cursor": "c", "meta": {}, "error": None,
    }
    rendered = wn.model_text(big, limit=500)
    assert len(rendered) <= 500
    assert "cursor=c" in rendered
