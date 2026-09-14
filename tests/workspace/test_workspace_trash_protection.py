"""回收站保护与操作事件/快照回归。

三条用户可见的安全约束：

1. ``.lumi_trash`` **默认不出现在 list**、**不能被 read 读取**、不参与 search/scan，
   只能由恢复/清理接口访问；
2. 读取/写入的版本（revision）由同一算法给出：整篇读完才给 revision，并附带
   "作为 expected_revision 传回"的提示；分页中间页不给（半截哈希会误导写入）；
3. 操作事件与 Job 快照只携带安全摘要（路径/状态/版本/影响面/审批/可回滚），
   **不含正文与原始参数**。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.contracts.operations import (
    OperationKind,
    RevisionRules,
    pending_approval_result,
    success_result,
)
from app.workspace.read import navigator as wn
from app.services.operation_events import (
    OPERATION_EVENT_COMPLETED,
    build_operation_event,
    event_fields_for_result,
    event_type_for_result,
)
from app.services.operation_snapshots import summary_from_result
from app.workspace.read.navigator import WorkspaceNavigatorService
from app.workspace.write.revision import revision_for_text
from app.workspace.write.trash import (
    TrashIndex,
    build_record,
    expired_records,
    guard_normal_operation,
    quota_plan,
    restore_update,
    summary_of,
)

from app.contracts.operations.delete import TrashRecord

READY = {"status_code": "WORKSPACE_READY", "server_name": "lumi_pc", "device_id": "dev-1"}
ADVERTISED = (
    {"name": "workspace_list"},
    {"name": "workspace_search"},
    {"name": "workspace_read"},
)


def _service(results=None, *, advertised=ADVERTISED):
    """工作区服务替身（与 test_workspace_navigator 同一注入点）。"""

    async def fake_list_tools(server_name):
        return list(advertised)

    async def fake_call_tool(server_name, tool_name, args):
        value = (results or {}).get(tool_name)
        if callable(value):
            return value(args)
        return value if value is not None else {"status": "success", "data": {}}

    return WorkspaceNavigatorService(
        user_id="u1",
        workspace_id="w1",
        conversation_id="c1",
        resolve_route=lambda: dict(READY),
        list_tools=fake_list_tools,
        call_tool=fake_call_tool,
    )


def _run(service, action, params=None):
    return asyncio.run(service.execute(action, params or {}))


# ── 1. 回收站保护 ────────────────────────────────────────────────


def test_list_hides_trash_entries_and_refuses_explicit_trash():
    entries = [
        {"name": "a.py", "path": "a.py", "type": "file", "size": 3, "mtime": 1},
        {"name": ".lumi_trash", "path": ".lumi_trash", "type": "dir", "size": 0, "mtime": 1},
        {"name": "index.json", "path": ".lumi_trash/index.json", "type": "file", "size": 9},
    ]
    service = _service({"workspace_list": {"status": "success", "data": {"entries": entries}}})
    payload = _run(service, "list", {})
    paths = [item["path"] for item in payload["data"]["entries"]]
    assert paths == ["a.py"], f"回收站不得出现在 list 结果里：{paths}"
    assert payload["meta"]["trash_hidden"] == 2

    blocked = _run(service, "list", {"path": ".lumi_trash"})
    assert blocked["status"] == "error"
    assert blocked["error"]["code"] == wn.TRASH_PATH_FORBIDDEN


def test_read_refuses_trash_paths():
    service = _service()
    payload = _run(service, "read", {"path": ".lumi_trash/files/abc/a.py"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == wn.TRASH_PATH_FORBIDDEN
    assert payload["error"]["suggested_action"]


def test_search_refuses_trash_scope_and_filters_trash_hits():
    blocked = _service()
    payload = _run(blocked, "search", {"query": "x", "search_path": ".lumi_trash"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == wn.TRASH_PATH_FORBIDDEN

    matches = [
        {"path": "src/a.py", "location": "line-1", "snippet": "x", "score": 2},
        {"path": ".lumi_trash/files/abc/b.py", "location": "line-1", "snippet": "x", "score": 1},
    ]
    service = _service({"workspace_search": {"status": "success", "data": {"matches": matches}}})
    result = _run(service, "search", {"query": "x"})
    paths = [item["path"] for item in result["data"]["matches"]]
    assert paths == ["src/a.py"], f"回收站命中必须过滤：{paths}"
    assert result["meta"]["trash_hidden"] == 1


def test_scan_refuses_trash_paths():
    service = _service()
    payload = _run(service, "scan", {"path": ".lumi_trash/files/abc/a.py"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == wn.TRASH_PATH_FORBIDDEN


def test_trash_guard_covers_normal_operations():
    assert guard_normal_operation(".lumi_trash") != ""
    assert guard_normal_operation(".lumi_trash/index.json") != ""
    assert guard_normal_operation("src/a.py") == ""


# ── 2. revision 暴露（写入/编辑的前置条件）───────────────────────


class _FakeReader:
    """替身 WorkspaceReader：直接给固定的一页内容（读取入口走它而不是聚合服务）。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def read(self, request, *, path="", cursor="", max_chars=0):
        return self._payload


def _patch_reader(monkeypatch, payload: dict) -> None:
    from app.workspace.read import reader as wr

    monkeypatch.setattr(wr.WorkspaceReader, "read", _FakeReader(payload).read)


def test_read_exposes_revision_when_whole_file_was_read(monkeypatch):
    text = "line1\nline2\n"
    service = _service(
        {
            "workspace_list": {
                "status": "success",
                "data": {"entries": [{"name": "a.py", "path": "a.py", "type": "file", "size": len(text)}]},
            },
        }
    )
    _patch_reader(
        monkeypatch,
        {
            "status": "success",
            "content": [{"source": "a.py", "location": "", "title": "a.py", "text": text}],
            "has_more": False,
            "meta": {"format": "text", "parser": "workspace_read", "workspace_version": 7},
        },
    )
    payload = _run(service, "read", {"path": "a.py"})
    assert payload["status"] == "ok"
    assert payload["meta"]["revision"] == revision_for_text(text)
    assert "expected_revision" in payload["meta"]["expected_revision_hint"]
    assert payload["meta"]["workspace_version"] == 7
    assert "revision" in payload["summary"]


def test_read_without_full_content_does_not_claim_a_revision(monkeypatch):
    service = _service(
        {
            "workspace_list": {
                "status": "success",
                "data": {"entries": [{"name": "big.txt", "path": "big.txt", "type": "file", "size": 999}]},
            },
        }
    )
    _patch_reader(
        monkeypatch,
        {
            "status": "partial",
            "content": [{"source": "big.txt", "location": "", "title": "big.txt", "text": "first page"}],
            "has_more": True,
            "cursor": "nav1:next",
            "meta": {"format": "text", "parser": "workspace_read", "workspace_version": 3},
        },
    )
    payload = _run(service, "read", {"path": "big.txt"})
    assert payload["status"] == "partial"
    assert "revision" not in payload["meta"], "分页中间页不得给出（会诱导拿半截版本去写）"


# ── 3. 回收站记录/保留期/配额 ────────────────────────────────────


def _record(entry_id: str, *, days_ago: int = 0, size: int = 10) -> TrashRecord:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return TrashRecord(
        entry_id=entry_id,
        logical_path=f"docs/{entry_id}.md",
        trash_path=f".lumi_trash/{entry_id}",
        bytes=size,
        deleted_at=moment.isoformat(),
        created_at=moment.isoformat(),
        expires_at=(moment + timedelta(days=7)).isoformat(),
        retention_days=7,
    )


def test_trash_index_round_trip_and_restore_bookkeeping():
    index = TrashIndex()
    index.add(_record("aaa"))
    index.add(_record("bbb"))
    loaded = TrashIndex.loads(index.dumps())
    assert {item.entry_id for item in loaded.records()} == {"aaa", "bbb"}

    updated = loaded.update("aaa", **restore_update(loaded.get("aaa"), revision="sha256:x:1"))
    assert updated is not None and updated.restorable is False
    assert updated.restore_revision == "sha256:x:1"
    assert loaded.remove("bbb") is not None
    assert loaded.get("bbb") is None
    # 损坏的索引不能阻断删除/恢复（按空索引继续）
    assert len(TrashIndex.loads("{not json")) == 0


def test_expired_records_and_quota_plan():
    records = [_record("old", days_ago=20), _record("new", days_ago=1)]
    expired = expired_records(records)
    assert [item.entry_id for item in expired] == ["old"]

    ok = quota_plan(records, incoming_bytes=10, policy=None)
    assert ok["allowed"] is True and ok["evictable"] == ["old"]

    blocked = quota_plan(records, incoming_bytes=10, policy=__import__(
        "app.contracts.operations", fromlist=["TrashPolicy"]
    ).TrashPolicy(max_items=1))
    assert blocked["allowed"] is False and "超过上限" in blocked["reason"]

    view = summary_of(records)
    # 保留期/上限与客户端一致（§11.4：7 天 / 500 条）
    assert view["count"] == 2 and view["retention_days"] == 7
    assert view["max_items"] == 500
    assert view["dir"] == ".lumi_trash" and view["index"].endswith("trash.json")


def test_build_record_carries_origin_task_and_retention():
    from app.contracts.operations import OperationContext

    context = OperationContext(
        workspace_id="w1", job_id="job-9", conversation_id="c1", user_id="u1", device_id="d1"
    )
    record = build_record(
        logical_path="./docs/a.md",
        entry_id="e1",
        revision="sha256:abc:3",
        bytes_size=3,
        context=context,
    )
    assert record.logical_path == "docs/a.md"
    # 客户端把内容直接 rename 到 .lumi_trash/<trash_id>（不再有 files/ 子目录）
    assert record.trash_path == ".lumi_trash/e1"
    assert record.job_id == "job-9" and record.task_id == "job-9"
    assert record.conversation_id == "c1" and record.device_id == "d1"
    assert record.restorable is True and record.retention_days == 7
    assert record.expires_at


def test_index_round_trip_uses_the_client_shape():
    """后端写出的索引必须是客户端能读的形状（trash_id/retention_days/entries）。"""
    index = TrashIndex([_record("aaa", size=42)], retention_days=7)
    payload = json.loads(index.dumps())
    assert payload["version"] == 1 and payload["retention_days"] == 7
    row = payload["entries"][0]
    assert row["trash_id"] == "aaa"
    assert row["logical_path"].endswith("aaa.md")
    assert row["bytes"] == 42
    assert "entry_id" not in row, "对外只暴露客户端字段名"
    # 反向：客户端写的索引同样能读回（trash_id → entry_id）
    reloaded = TrashIndex.loads(index.dumps())
    assert [item.entry_id for item in reloaded.records()] == ["aaa"]
    assert reloaded.retention_days == 7


# ── 4. 操作事件与快照只带安全摘要 ────────────────────────────────


def _result(**overrides):
    from app.contracts.operations import ChangeSummary

    payload = {
        "kind": OperationKind.EDIT,
        "logical_path": "src/a.py",
        "new_revision": revision_for_text("new\n"),
        "changes": ChangeSummary(modified=["src/a.py"], added_lines=1, removed_lines=1),
        "job_id": "job-1",
    }
    payload.update(overrides)
    return success_result(None, **payload)


def test_operation_event_fields_are_safe_and_statusful():
    result = _result()
    fields = event_fields_for_result(result)
    assert fields["operation"] == "edit"
    assert fields["status"] == "success"
    assert fields["changed_files"] == ["src/a.py"]
    assert event_type_for_result(result) == OPERATION_EVENT_COMPLETED

    event = build_operation_event(OPERATION_EVENT_COMPLETED, job_id="job-1", **fields)
    assert "content" not in event and "arguments" not in event
    assert event["operation"] == "edit" and event["job_id"] == "job-1"


def test_operation_summary_snapshot_has_no_content():
    summary = summary_from_result(_result(stats={"files": 1, "bytes": 12}))
    assert summary["operation"] == "edit"
    assert summary["changed_files"] == ["src/a.py"]
    assert summary["rollback_available"] is True
    assert summary["bytes"] == 12
    # 白名单之外的键（例如正文/参数）不会进快照
    assert "content" not in summary and "arguments" not in summary


def test_revision_rules_used_by_read_and_write_are_the_same_algorithm():
    text = "hello\n"
    assert revision_for_text(text) == RevisionRules.for_file(text)
    assert RevisionRules.matches(RevisionRules.for_file(text).split(":")[1], revision_for_text(text))


class _FakeRedis:
    """极简 Redis 替身：只实现快照用到的 hset/hvals/expire。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    async def hset(self, key, field, value):  # noqa: ANN001
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hvals(self, key):  # noqa: ANN001
        return list(self.hashes.get(key, {}).values())

    async def expire(self, key, ttl):  # noqa: ANN001
        return True


def test_operation_snapshot_round_trip_and_aggregate_view(monkeypatch):
    """Job 快照：同一 Job 的操作摘要可恢复，并聚合成"操作面板"需要的字段。"""
    from app.services import operation_snapshots as snapshots

    fake = _FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)

    pending = pending_approval_result(
        None,
        kind=OperationKind.DELETE,
        logical_path="docs/b.md",
        plan={"files": 1, "bytes": 5},
        job_id="job-1",
    )
    done = _result(stats={"files": 1, "bytes": 12})
    assert asyncio.run(snapshots.record_operation_summary(pending)) is True
    assert asyncio.run(snapshots.record_operation_summary(done)) is True

    view = asyncio.run(snapshots.operation_summary_view("job-1"))
    assert [item["operation"] for item in view["operations"]] == ["edit", "delete"]
    assert view["latest"]["operation"] == "edit"
    assert set(view["changed_files"]) == {"src/a.py", "docs/b.md"}
    assert view["approval_state"] == "pending", "有待审批的操作时必须暴露审批状态"
    assert view["rollback_available"] is True
    assert view["no_change"] is False
    # 空 job / Redis 不可用时都必须安全降级
    assert asyncio.run(snapshots.operation_summaries_for_job("")) == []
    monkeypatch.setattr(
        "app.core.redis.get_redis", lambda: (_ for _ in ()).throw(RuntimeError("redis down"))
    )
    assert asyncio.run(snapshots.operation_summary_view("job-1"))["operations"] == []
