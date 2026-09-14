"""读取分页契约回归测试：**单次返回粒度** ≠ **文件可读取总量**。

用户报告：读一份 25 页的 PPT，读到 25 页就停了，即使要求继续也不继续。这里把
"一次返回多少"和"这份文件能读多少"彻底分开验证：

* 文件比一页长时，一次 read 调用内部按页连续拉取，直到解析器说没有更多；
* 页数上限（``MAX_READ_PAGES_PER_CALL``）用尽时返回 ``partial`` +
  ``has_more=true`` + ``cursor`` + ``page_budget_exhausted=true``，**不谎报读完**；
* 拿 cursor 继续调用可以一直读到文件真正结束（不会因为固定页数提前结束）；
* ``read_full=false`` 时保持旧的"一次一页"行为；
* 覆盖读取 Agent 必须把 has_more 的文件记进 ``truncated_files``，且不声称全量覆盖。
"""

from __future__ import annotations

import asyncio

from app.workspace.read import navigator as wn
from app.workspace.read.navigator import WorkspaceNavigatorService

READY = {"status_code": "WORKSPACE_READY", "server_name": "lumi_pc", "device_id": "dev-1"}
ADVERTISED = (
    {"name": "workspace_list"},
    {"name": "workspace_stat"},
    {"name": "workspace_content_extract"},
)


class _PagedReader:
    """模拟 WorkspaceReader：按页返回，每页有独立 location 与 cursor。"""

    def __init__(self, total_sections: int, *, per_page: int = 5, text_size: int = 8):
        self.total = total_sections
        self.per_page = per_page
        self.text_size = text_size
        self.calls: list[dict] = []

    async def read(self, request, *, path="", cursor="", max_chars=12000):
        self.calls.append({"path": path, "cursor": cursor, "max_chars": max_chars})
        start = int(cursor.split("-")[-1]) if cursor else 0
        end = min(self.total, start + self.per_page)
        sections = [
            {
                "source": path,
                "location": f"slide-{index + 1}",
                "title": f"第{index + 1}页",
                "text": f"第{index + 1}页正文" + "内容" * self.text_size,
            }
            for index in range(start, end)
        ]
        has_more = end < self.total
        return {
            "status": "partial" if has_more else "success",
            "summary": f"已读取 {path}（{len(sections)} 段）",
            "content": sections,
            "has_more": has_more,
            "cursor": f"reader-{end}" if has_more else None,
            "meta": {"format": "pptx", "parser": "workspace_content_extract", "workspace_version": 3},
        }


def _service(reader: _PagedReader, **kwargs):
    async def list_tools(_server):
        return list(ADVERTISED)

    async def call_tool(_server, tool_name, args):
        if tool_name == "workspace_stat":
            return {"status": "success", "data": {"kind": "file", "size": 40960}}
        return {"status": "success", "data": {}}

    service = WorkspaceNavigatorService(
        user_id="u1",
        workspace_id="w1",
        conversation_id="c1",
        resolve_route=lambda: dict(READY),
        list_tools=list_tools,
        call_tool=call_tool,
        **kwargs,
    )
    service._reader_override = reader
    return service


def _run(service, action, params=None):
    return asyncio.run(service.execute(action, params or {}))


def _install_paged_reader(monkeypatch, reader: _PagedReader) -> None:
    async def fake_read(self, request, *, path="", cursor="", max_chars=12000):
        return await reader.read(request, path=path, cursor=cursor, max_chars=max_chars)

    monkeypatch.setattr(
        "app.workspace.read.reader.WorkspaceReader.read", fake_read
    )


# ── 核心：25 页 PPT 必须能一次读到超过 25 页的量 ─────────────

def test_long_file_is_read_across_pages_up_to_the_page_budget(monkeypatch):
    """单次调用按页连续拉取（默认 read_full），不会读一页就停。"""
    reader = _PagedReader(total_sections=25, per_page=5)
    _install_paged_reader(monkeypatch, reader)
    service = _service(reader)

    payload = _run(service, "read", {"path": "答辩.pptx"})
    # 5 页/次 × 8 页上限 = 40 段 > 25 段，因此一次调用就把整份读完。
    assert payload["status"] == "ok"
    assert payload["has_more"] is False
    assert len(payload["data"]["sections"]) == 25
    assert payload["meta"]["pages_read"] == 5
    assert payload["meta"]["page_budget_exhausted"] is False
    # 位置连续、无重复
    locations = [item["location"] for item in payload["data"]["sections"]]
    assert locations == [f"slide-{i}" for i in range(1, 26)]
    assert "已读到文件结尾" in payload["summary"]


def test_page_budget_does_not_claim_completion(monkeypatch):
    """超过单次页数上限时必须返回 partial + cursor，并标明是预算用尽。"""
    reader = _PagedReader(total_sections=500, per_page=5)   # 100 页
    _install_paged_reader(monkeypatch, reader)
    service = _service(reader)

    payload = _run(service, "read", {"path": "超长文档.pptx"})
    assert payload["status"] == "partial"
    assert payload["has_more"] is True
    assert payload["cursor"]
    assert payload["meta"]["pages_read"] == wn.MAX_READ_PAGES_PER_CALL
    assert payload["meta"]["page_budget_exhausted"] is True
    assert "文件尚未读完" in payload["summary"]
    assert "已读完" not in payload["summary"]


def test_continuation_with_cursor_reaches_the_real_end(monkeypatch):
    """反复用 cursor 继续，可以一直读到文件真正结束（不因固定页数提前结束）。"""
    reader = _PagedReader(total_sections=500, per_page=5)   # 100 页
    _install_paged_reader(monkeypatch, reader)
    service = _service(reader)

    collected: list[str] = []
    cursor = ""
    rounds = 0
    payload = _run(service, "read", {"path": "超长文档.pptx"})
    while True:
        rounds += 1
        collected.extend(item["location"] for item in payload["data"]["sections"])
        if not payload["has_more"]:
            break
        assert payload["cursor"], "has_more=true 必须给出可用 cursor"
        cursor = payload["cursor"]
        payload = _run(service, "read", {"path": "超长文档.pptx", "cursor": cursor})
        assert rounds <= 25, "cursor 续读不应无限循环"

    assert len(collected) == 500
    assert len(set(collected)) == 500, "分页边界不得重复"
    # 100 页 ÷ 8 页/次 = 13 次调用（最后一次不足 8 页）
    assert rounds == 13


def test_read_full_false_keeps_single_page_behaviour(monkeypatch):
    reader = _PagedReader(total_sections=25, per_page=5)
    _install_paged_reader(monkeypatch, reader)
    service = _service(reader)

    payload = _run(service, "read", {"path": "答辩.pptx", "read_full": False})
    assert payload["status"] == "partial"
    assert len(payload["data"]["sections"]) == 5
    assert payload["has_more"] is True
    assert payload["meta"]["read_full_requested"] is False


def test_page_chars_is_granularity_not_total_cap(monkeypatch):
    """meta 必须能区分"每页多少字符"和"本次总共读了多少"。"""
    reader = _PagedReader(total_sections=25, per_page=5, text_size=400)
    _install_paged_reader(monkeypatch, reader)
    service = _service(reader)
    payload = _run(service, "read", {"path": "答辩.pptx"})

    assert payload["meta"]["page_chars"] == wn.READ_MAX_CHARS
    assert payload["meta"]["pages_read"] == 5
    # 一次调用读到的总量可以远超"每页字符数"——这正是两者解耦的证明。
    assert payload["meta"]["char_count"] > wn.READ_MAX_CHARS
    assert len(payload["data"]["sections"]) == 25


def test_continuation_cursor_also_reads_to_end_by_default(monkeypatch):
    reader = _PagedReader(total_sections=25, per_page=5)
    _install_paged_reader(monkeypatch, reader)
    service = _service(reader)
    payload = _run(service, "read", {"path": "答辩.pptx", "cursor": "reader-5"})

    assert payload["meta"]["continued_from_cursor"] is True
    assert len(payload["data"]["sections"]) == 20
    assert payload["has_more"] is False


def test_reader_cursor_budget_is_respected():
    """页数上限常量必须显式存在，且大于 1（否则等于退回单页）。"""
    assert wn.MAX_READ_PAGES_PER_CALL > 1


def test_read_to_end_uses_extended_request_budget(monkeypatch):
    """明确完整读取时可跨多个默认批次，但仍受 request 级安全预算保护。"""
    reader = _PagedReader(total_sections=500, per_page=5)
    _install_paged_reader(monkeypatch, reader)
    service = _service(reader)
    payload = _run(service, "read", {"path": "整份.pptx", "read_to_end": True})
    assert payload["meta"]["page_limit"] == wn.MAX_READ_PAGES_PER_REQUEST
    assert payload["has_more"] is False
    assert len(payload["data"]["sections"]) == 500
